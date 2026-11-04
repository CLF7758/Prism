"""The row picture of a material must not decode the original picture.

Measured on the 100k project: every material row asked for its own little
sphere preview, and every one of them decoded the full picture of the
model's textures.  A 4K picture costs about four hundred milliseconds and
160 MB of pixels to decode, and none of that can be seen in a 128 pixel
ball.  The row pictures of the whole project cost 9.1 s of native render
time, and the native helper built a new OpenGL context for every row.

These tests pin the two properties that fix it:

  * the row picture is painted from a small copy of the picture, and that
    copy is written once and read from then on,
  * the native helper keeps one off-screen renderer for all row pictures, so
    the second row of the same picture does not decode it again.
"""
import json
import threading
import time
from pathlib import Path

from PIL import Image

from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import (
    MaterialDefinition, MaterialTarget, TextureUse)
from prism.model_library.multimaterial.scheduling import MaterialScheduler


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


def close(service):
    for pool in (service._pool, service._render_pool, service._texture_pool,
                 service._thumbnail_pool):
        pool.shutdown()


class Controller:
    """A native helper that only records what it was asked to render."""

    def __init__(self):
        self.payloads = []

    def submit(self, request, **kwargs):
        self.payloads.append(kwargs.get('payload') or {})
        return 'job-%d' % len(self.payloads)

    def wait(self, job, timeout=0):
        return {'state': 'completed'}

    def forget(self, job):
        pass


def make_definition(texture_path):
    return ProjectMaterialService._normalise(MaterialDefinition(
        material_id='m-1', source_key='key', name='Wood', workflow='gltf',
        textures=(TextureUse(slot='basecolor', resource_id='sha256:wood',
                              channels='RGB', texcoord=0, color_space='srgb',
                              source_locator=texture_path.name),),
        properties_json=json.dumps({})))


def test_the_row_picture_is_painted_from_a_small_copy(tmp_path):
    """The picture handed to the native helper must be the small one."""
    original = tmp_path / 'wood.png'
    Image.new('RGB', (1024, 1024), (120, 80, 40)).save(original)
    service, published = build_service(tmp_path)
    controller = Controller()
    service.controller = controller
    service.cover_controller = controller
    definition = make_definition(original)
    target = MaterialTarget('session', 1, 'model-a', 1, 1, 1)
    service._load = lambda _target: {'catalog': {}, 'state': None, 'path': 'model.glb',
                                     'payload': {'name': 'Wood'}, 'paths': {
                                         'sha256:wood': str(original)}}
    service._resolved = lambda item, working_id, state=None: definition
    try:
        service.thumbnail(target, 'working:w-1')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not controller.payloads:
            time.sleep(.02)
        assert controller.payloads, published
        painted = controller.payloads[0]['material']
        assert painted['maps']['basecolor']['path'] != str(original), \
            'the row picture still uses the original picture'
        with Image.open(painted['maps']['basecolor']['path']) as small:
            assert max(small.size) <= 512, small.size
        assert Path(painted['maps']['basecolor']['path']).stat().st_size < original.stat().st_size
    finally:
        close(service)


def test_the_small_copy_is_written_once(tmp_path):
    """The copy is a cache: the second request must not write it again."""
    original = tmp_path / 'wood.png'
    Image.new('RGB', (256, 256), (10, 20, 30)).save(original)
    service, _ = build_service(tmp_path)
    try:
        first = service._small_texture(str(original))
        assert first, 'the small copy was not made'
        stamp = Path(first).stat().st_mtime_ns
        second = service._small_texture(str(original))
        assert second == first
        assert Path(first).stat().st_mtime_ns == stamp, \
            'the small copy was written twice'
        assert len(list(tmp_path.glob('*.small.png'))) == 1
    finally:
        close(service)


def test_a_lookup_that_may_not_write_leaves_the_cache_alone(tmp_path):
    """Looking for a copy must not create one."""
    original = tmp_path / 'wood.png'
    Image.new('RGB', (256, 256), (10, 20, 30)).save(original)
    service, _ = build_service(tmp_path)
    try:
        assert service._small_texture(str(original), create=False) is None
        assert not list(tmp_path.glob('*.small.png'))
        service._small_texture(str(original))
        assert service._small_texture(str(original), create=False)
    finally:
        close(service)


def test_a_missing_picture_does_not_break_the_row_picture(tmp_path):
    """A picture that cannot be read must not stop the row."""
    service, _ = build_service(tmp_path)
    try:
        assert service._small_texture(str(tmp_path / 'gone.png')) is None
        assert service._small_texture('') is None
    finally:
        close(service)


def test_the_native_helper_keeps_one_renderer_for_the_row_pictures(tmp_path):
    """Two row pictures of the same picture must not decode it twice.

    A new renderer per row means a new OpenGL context and an empty texture
    cache, so every row decodes the picture again.  The measured cost was
    0.3 s to 1.7 s per row for a 4K picture.
    """
    from prism.model_library.contracts import ModelJobKind, ModelJobRequest, ModelRequestIdentity
    from prism.model_library.native_jobs import NativeModelJobController, RuntimeConfig
    from prism.model_library.native_protocol import WORKER_NAME_PREVIEW

    picture = tmp_path / 'wood.png'
    Image.new('RGB', (512, 512), (120, 80, 40)).save(picture)
    material = dict(color=[1, 0, 0], roughness=.5, metallic=0, opacity=1,
                    alpha_mode='OPAQUE', emissive_factor=[0, 0, 0],
                    maps={'basecolor': dict(path=str(picture), channels='RGB',
                                            texcoord=0, color_space='srgb',
                                            uv_transform=[], normal_convention='')})
    controller = NativeModelJobController(RuntimeConfig(
        cache_root=tmp_path / 'native', worker_target='prism.components.material_preview',
        worker_worker_args=(), worker_name=WORKER_NAME_PREVIEW,
        env={'QT_QPA_PLATFORM': 'offscreen'}, shutdown_timeout_s=2))
    try:
        timings = []
        for index in range(2):
            identity = ModelRequestIdentity('session', 1, 'model', str(index), 1, 0, 0)
            job = controller.submit(ModelJobRequest(
                identity, ModelJobKind.PREVIEW, str(picture)), kind='material-test',
                payload=dict(action='thumbnail', material=material,
                             output=str(tmp_path / ('sphere-%d.png' % index))))
            result = controller.wait(job, timeout=60)
            controller.forget(job)
            assert result and result['state'] == 'completed', result
            timings.append((result.get('timing') or {}))
        with Image.open(tmp_path / 'sphere-1.png') as image:
            assert image.size == (128, 128)
        assert timings[1].get('texture_reuses', 0) >= 1, timings
        assert timings[1].get('image_decodes', 0) == 0, timings
    finally:
        controller.shutdown()
