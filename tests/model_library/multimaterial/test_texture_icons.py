"""The texture slot icons must not fill the material queue.

The inspector asks for one 64x64 channel icon per texture slot, but the
pictures behind the slots are the heavy originals: one 8K texture costs
about two seconds to decode.  One job per slot put dozens of decodes into
the same single-threaded lane as the native renders, so the last icon
arrived twelve seconds after the click.

These tests pin the three properties that fix it:

  * one material paints all of its slots with one queued job,
  * the icon is written next to the texture and reused afterwards,
  * the icon job runs on its own lane, so a busy render cannot hold it up.
"""
import io
import time

from PIL import Image

from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import MaterialTarget, TextureUse
from prism.model_library.multimaterial.scheduling import MaterialScheduler


def read_icon(data):
    """The published icon as a picture; the panel loads it the same way."""
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def make_target(model_id='model-a'):
    return MaterialTarget('session', 1, model_id, 1, 1, 1)


def write_texture(path, size=8, colour=(200, 40, 40, 255)):
    with Image.new('RGBA', (size, size), colour) as image:
        image.save(path, format='PNG')


def build_service(tmp_path, slots=('basecolor', 'normal', 'roughness')):
    """A service whose material has ``slots`` texture bindings."""
    service = object.__new__(ProjectMaterialService)
    service._closed = False
    service._cancelled = set()
    service._jobs = {}
    service._job_requests = {}
    service._texture_jobs = {}
    service._lock = __import__('threading').RLock()
    service._root = tmp_path
    service._texture_pool = MaterialScheduler()
    service._render_pool = MaterialScheduler()
    from prism.material_cache import MaterialCache
    service._texture_icons = MaterialCache(128, 8 * 1024 * 1024,
                                          cost=lambda key, value: len(value))
    published = []
    service._publish = lambda **event: published.append(event)

    paths = {}
    uses = []
    for index, slot in enumerate(slots):
        path = tmp_path / ('tex-%s.png' % slot)
        write_texture(path, colour=(10 * index, 40, 90, 255))
        resource = 'sha256:%s' % slot
        paths[resource] = str(path)
        uses.append(TextureUse(slot, resource, 'RGB', 0, 'linear'))

    class Definition:
        textures = tuple(uses)

    service._load = lambda target: {'paths': paths}
    service._resolved = lambda item, working_id, state=None: Definition()
    return service, published, paths


def test_one_material_paints_every_slot_with_one_queued_job(tmp_path):
    service, published, _ = build_service(tmp_path)
    try:
        # The inspector asks slot by slot; every slot of the same material.
        for slot in ('basecolor', 'normal', 'roughness'):
            service.texture_thumbnail(make_target(), 'w-1', slot)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len([e for e in published if e.get('kind') == 'texture_image']) >= 3:
                break
            time.sleep(.02)
        icons = [e for e in published if e.get('kind') == 'texture_image']
        slots = {icon['slot'] for icon in icons}
        assert slots == {'basecolor', 'normal', 'roughness'}
        for icon in icons:
            width, height = read_icon(icon['data'])
            assert max(width, height) <= 64
    finally:
        service._texture_pool.shutdown()
        service._render_pool.shutdown()


def test_the_icon_is_written_next_to_the_texture_and_reused(tmp_path):
    service, published, paths = build_service(tmp_path)
    try:
        service.texture_thumbnail(make_target(), 'w-1', 'basecolor')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if [e for e in published if e.get('kind') == 'texture_image']:
                break
            time.sleep(.02)
        icons = [e for e in published if e.get('kind') == 'texture_image']
        assert icons, 'the icon was never published'
        written = list(tmp_path.glob('*.icon.png'))
        assert written, 'the icon was not cached next to the texture'
        assert written[0].read_bytes() == icons[0]['data']

        # A second session must read the icon, not decode the original again.
        service2, published2, _ = build_service(tmp_path)
        try:
            service2.texture_thumbnail(make_target(), 'w-1', 'basecolor')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if [e for e in published2 if e.get('kind') == 'texture_image']:
                    break
                time.sleep(.02)
            again = [e for e in published2 if e.get('kind') == 'texture_image']
            assert again and again[0]['data'] == icons[0]['data']
        finally:
            service2._texture_pool.shutdown()
            service2._render_pool.shutdown()
    finally:
        service._texture_pool.shutdown()
        service._render_pool.shutdown()


def test_a_busy_render_lane_does_not_hold_up_the_icons(tmp_path):
    service, published, _ = build_service(tmp_path)
    try:
        # A render that takes a long time occupies the render lane.
        release = __import__('threading').Event()
        service._render_pool.submit(release.wait, 30)
        service.texture_thumbnail(make_target(), 'w-1', 'basecolor')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if [e for e in published if e.get('kind') == 'texture_image']:
                break
            time.sleep(.02)
        assert [e for e in published if e.get('kind') == 'texture_image'], \
            'the icon waited for the render lane'
    finally:
        release.set()
        service._texture_pool.shutdown()
        service._render_pool.shutdown()
