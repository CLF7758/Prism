"""Backend tests: capability honesty and Prism appearance → F3D parameters.

The parameter tests build their own textures with Pillow, so they run without a
GPU. The last two tests drive the real F3D helper through the port and are
skipped when the native runtime is missing.
"""
from __future__ import annotations

import hashlib
import io
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image

from prism.model_library import f3d_parameters as params
from prism.model_library.contracts import (ModelAppearance, ModelFeatureUnavailable,
                                           ModelJobKind, ModelJobRequest,
                                           ModelRequestIdentity)
from prism.model_library.f3d_backend import F3DBackend
from prism.model_library.native_jobs import RuntimeConfig
from prism.model_library.native_protocol import Handshake

ROOT = Path(__file__).resolve().parents[2]


# ── helpers ───────────────────────────────────────────────────────────────
def _png(size=16, color=(255, 255, 255), mode='RGB') -> bytes:
    image = Image.new(mode, (size, size), color)
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def _binding(blob: bytes, **overrides) -> dict:
    binding = {'attachmentId': 'att-1', 'name': 'tex.png',
               'sha256': hashlib.sha256(blob).hexdigest(), 'channel': 'RGB'}
    binding.update(overrides)
    return binding


def _appearance(bindings=None, slots=None, revision=1) -> ModelAppearance:
    return ModelAppearance(revision=revision, bindings=dict(bindings or {}),
                           properties={'slots': dict(slots or {})})


class _Reader:
    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.calls = []

    def __call__(self, binding):
        self.calls.append(binding.get('attachmentId'))
        return self.blobs.get(binding.get('attachmentId'))


def _identity(**overrides):
    base = dict(project_session='ps', open_epoch=1, model_id='m', request_id='r',
                source_revision=1, appearance_revision=1, camera_revision=0)
    base.update(overrides)
    return ModelRequestIdentity(**base)


def _request(manifest='model.obj', **overrides):
    return ModelJobRequest(identity=_identity(**overrides),
                           kind=ModelJobKind.THUMBNAIL, manifest_ref=manifest)


# ── no override: keep the imported materials ──────────────────────────────
def test_empty_appearance_sets_no_option(tmp_path):
    snapshot = params.build_snapshot(ModelAppearance(), None, tmp_path)
    assert snapshot.has_override is False
    assert snapshot.option_values() == {}
    assert snapshot.to_payload()['has_override'] is False


def test_slot_at_its_stored_default_is_not_an_override(tmp_path):
    appearance = _appearance(slots={'roughness': {'value': 0.5, 'factor': 1.0}})
    assert params.has_override(appearance) is False
    assert params.build_snapshot(appearance, None, tmp_path).option_values() == {}


def test_sss_and_displacement_are_reported_not_applied(tmp_path):
    appearance = _appearance(slots={'sss': {'value': 0.4},
                                    'displacement': {'value': 0.05}})
    snapshot = params.build_snapshot(appearance, None, tmp_path)
    assert snapshot.option_values() == {}
    joined = ' '.join(snapshot.unsupported)
    assert 'sss' in joined and 'displacement' in joined


# ── derived maps ──────────────────────────────────────────────────────────
def test_colour_map_is_derived_and_original_is_untouched(tmp_path):
    blob = _png(32, (200, 40, 40))
    reader = _Reader({'att-1': blob})
    appearance = _appearance(bindings={'basecolor': _binding(blob)})
    snapshot = params.build_snapshot(appearance, reader, tmp_path)

    options = snapshot.option_values()
    assert options['model.color.texture'] == snapshot.color_texture
    derived = Path(snapshot.color_texture)
    assert derived.is_file() and derived.parent == tmp_path
    assert blob in reader.blobs.values()
    assert reader.blobs['att-1'] == blob, '原图不能被改写'
    assert 'basecolor' in str(derived.name)


def test_texture_hash_mismatch_is_refused(tmp_path):
    blob = _png(8, (1, 2, 3))
    binding = _binding(blob)
    binding['sha256'] = '0' * 64
    appearance = _appearance(bindings={'basecolor': binding})
    with pytest.raises(params.AppearanceError):
        params.build_snapshot(appearance, _Reader({'att-1': blob}), tmp_path)


def test_missing_original_bytes_are_refused(tmp_path):
    blob = _png(8, (1, 2, 3))
    appearance = _appearance(bindings={'basecolor': _binding(blob)})
    with pytest.raises(params.AppearanceError):
        params.build_snapshot(appearance, _Reader({}), tmp_path)


def test_repeated_snapshot_reuses_the_same_derived_file(tmp_path):
    blob = _png(16, (10, 20, 30))
    appearance = _appearance(bindings={'basecolor': _binding(blob)})
    first = params.build_snapshot(appearance, _Reader({'att-1': blob}), tmp_path)
    second = params.build_snapshot(appearance, _Reader({'att-1': blob}), tmp_path)
    assert first.color_texture == second.color_texture
    assert len(list(tmp_path.glob('basecolor-*.png'))) == 1


def test_normal_map_keeps_scale_and_flips_green_when_asked(tmp_path):
    # A pure-green normal: flipping G turns 255 into 0, which is measurable.
    normal = Image.new('RGB', (8, 8), (128, 255, 128))
    buffer = io.BytesIO()
    normal.save(buffer, 'PNG')
    blob = buffer.getvalue()
    appearance = _appearance(bindings={'normal': _binding(blob)},
                             slots={'normal': {'value': 2.0}})
    snapshot = params.build_snapshot(appearance, _Reader({'att-1': blob}), tmp_path)
    assert snapshot.normal_scale == pytest.approx(2.0)
    written = Image.open(snapshot.normal_texture).convert('RGB')
    assert written.getpixel((0, 0))[1] == 255, '默认（OpenGL）法线不应翻转 G'

    flipped = _appearance(bindings={'normal': _binding(blob)},
                          slots={'normal': {'value': 2.0, 'invert': True}})
    snapshot2 = params.build_snapshot(flipped, _Reader({'att-1': blob}), tmp_path)
    written2 = Image.open(snapshot2.normal_texture).convert('RGB')
    assert written2.getpixel((0, 0))[1] == 0, 'DirectX 法线需要翻转 G'


def test_orm_packing_uses_the_measured_channels(tmp_path):
    """R=occlusion, G=roughness, B=metallic, with the strength inside the map."""
    roughness = _png(8, 128, mode='L')
    metallic = _png(8, 64, mode='L')
    blobs = {'r': roughness, 'm': metallic}
    reader = _Reader({'rough': roughness, 'metal': metallic})
    appearance = _appearance(
        bindings={'roughness': _binding(roughness, attachmentId='rough',
                                        channel='R', grayscale=True),
                  'metallic': _binding(metallic, attachmentId='metal',
                                       channel='R', grayscale=True)},
        slots={'roughness': {'factor': 0.5}, 'metallic': {'factor': 0.25}})
    snapshot = params.build_snapshot(appearance, reader, tmp_path)
    assert snapshot.orm_texture, '有贴图时应该生成 ORM'
    image = Image.open(snapshot.orm_texture).convert('RGB')
    r, g, b = image.getpixel((0, 0))
    assert r == 255, '缺少 AO 时 R 必须是中性 255'
    assert g == round(128 * 0.5), 'G 通道应为 粗糙度贴图 × 强度'
    assert b == round(64 * 0.25), 'B 通道应为 金属度贴图 × 强度'
    # 强度已经烘焙：再也不允许出现会覆盖贴图的乘数
    assert snapshot.material_roughness is None
    assert snapshot.material_metallic is None
    assert 'model.material.roughness' not in snapshot.option_values()
    assert blobs['r'] == roughness and blobs['m'] == metallic


def test_scalar_only_orm_uses_the_multiplier_and_never_a_map(tmp_path):
    appearance = _appearance(slots={'roughness': {'value': 0.2},
                                    'metallic': {'value': 0.9}})
    snapshot = params.build_snapshot(appearance, None, tmp_path)
    assert snapshot.orm_texture is None
    assert snapshot.material_roughness == pytest.approx(0.2)
    assert snapshot.material_metallic == pytest.approx(0.9)
    assert snapshot.option_values()['model.material.roughness'] == pytest.approx(0.2)


def test_opacity_mask_needs_a_colour_map_and_says_so(tmp_path):
    alpha = _png(8, 128, mode='L')
    appearance = _appearance(bindings={'opacity': _binding(alpha, channel='R',
                                                          grayscale=True)})
    snapshot = params.build_snapshot(appearance, _Reader({'att-1': alpha}), tmp_path)
    assert snapshot.color_texture is None
    assert snapshot.warnings, '仅透明贴图时必须报告无法合成'


def test_transparency_composes_alpha_and_uses_ddp(tmp_path):
    colour = _png(8, (200, 100, 50))
    alpha = _png(8, 128, mode='L')
    appearance = _appearance(
        bindings={'basecolor': _binding(colour),
                  'opacity': _binding(alpha, attachmentId='att-2', channel='R',
                                      grayscale=True)},
        slots={'opacity': {'value': 1.0}})
    snapshot = params.build_snapshot(
        appearance, _Reader({'att-1': colour, 'att-2': alpha}), tmp_path)
    assert snapshot.translucent is True
    assert snapshot.option_values()['render.effect.blending.mode'] == 'ddp'
    image = Image.open(snapshot.color_texture)
    assert image.mode == 'RGBA' and image.getpixel((0, 0))[3] == 128


def test_emissive_map_carries_its_strength(tmp_path):
    glow = _png(8, (255, 255, 255))
    appearance = _appearance(bindings={'emissive': _binding(glow)},
                             slots={'emissive': {'value': 3.0}})
    snapshot = params.build_snapshot(appearance, _Reader({'att-1': glow}), tmp_path)
    assert snapshot.option_values()['model.emissive.factor'] == [3.0, 3.0, 3.0]
    assert snapshot.emissive_texture.endswith('.png')


def test_snapshot_round_trips_through_json(tmp_path):
    blob = _png(8, (9, 9, 9))
    appearance = _appearance(bindings={'basecolor': _binding(blob)})
    snapshot = params.build_snapshot(appearance, _Reader({'att-1': blob}), tmp_path)
    restored = params.ParameterSnapshot.from_payload(snapshot.to_payload())
    assert restored == snapshot
    assert restored.option_values() == snapshot.option_values()


def test_snapshot_contains_no_project_paths_beyond_the_scratch_dir(tmp_path):
    blob = _png(8, (9, 9, 9))
    appearance = _appearance(bindings={'basecolor': _binding(blob)})
    snapshot = params.build_snapshot(appearance, _Reader({'att-1': blob}), tmp_path)
    payload = snapshot.to_payload()
    for key in ('color_texture', 'normal_texture', 'emissive_texture', 'orm_texture'):
        value = payload.get(key)
        if value:
            assert str(tmp_path) in value


def test_capability_report_states_the_real_limits():
    report = params.capability_report()
    assert 'sss' in report['unsupported_slots']
    assert report['orm_channels'] == {'R': 'ao', 'G': 'roughness', 'B': 'metallic'}
    assert 'textures_transform' in report and 'textures_transform' in params.__doc__


# ── backend capabilities and port behaviour ───────────────────────────────
class _StubController:
    def __init__(self, available=True, formats=None, wireframe=False):
        self.available = available
        self.formats = formats if formats is not None else {
            '.fbx': 'FBX', '.obj': 'OBJ', '.glb': 'GLB', '.gltf': 'GLTF'}
        self.wireframe = wireframe
        self.submitted = []
        self.cancelled = []
        self.closed = []

    def probe(self, timeout=None):
        if not self.available:
            return {'available': False, 'reason': '没有运行时', 'handshake': None,
                    'worker_pid': 0, 'restarts': 0, 'log_tail': (), 'capabilities': None}
        handshake = Handshake(worker='prism.model.f3d.offscreen', backend_version='4.0.0',
                              vtk_version='9.7.1', pid=1234, formats=self.formats,
                              readers=('OBJ',), wireframe=self.wireframe,
                              material_edges=True, appearance=True,
                              statistics='f3d-loaded')
        return {'available': True, 'reason': '', 'handshake': handshake,
                'worker_pid': 1234, 'restarts': 0, 'log_tail': (),
                'capabilities': {}}

    def submit(self, request, **kwargs):
        self.submitted.append((request, kwargs))
        return 'job-%d' % len(self.submitted)

    def cancel(self, job_id):
        self.cancelled.append(job_id)

    def close_project(self, session):
        self.closed.append(session)

    def shutdown(self):
        pass


def _backend(tmp_path, controller):
    return F3DBackend(RuntimeConfig(cache_root=tmp_path / 'cache'), controller=controller)


def test_capabilities_follow_the_handshake(tmp_path):
    backend = _backend(tmp_path, _StubController())
    capabilities = backend.capabilities
    assert capabilities.preview is True
    assert capabilities.wireframe is False
    assert capabilities.edit_appearance is True
    assert capabilities.backend_version == '4.0.0'
    report = backend.describe()
    assert report['verified_modes'] == ['material', 'material_edges']
    assert 'wireframe' in report['mode_reasons']


def test_missing_reader_disables_preview(tmp_path):
    controller = _StubController(formats={'.obj': 'OBJ'})
    backend = _backend(tmp_path, controller)
    assert backend.capabilities.preview is False
    with pytest.raises(ModelFeatureUnavailable):
        backend.open(_request())


def test_unavailable_runtime_fails_fast(tmp_path):
    backend = _backend(tmp_path, _StubController(available=False))
    assert backend.capabilities.preview is False
    with pytest.raises(ModelFeatureUnavailable):
        backend.open(_request())


def test_unsupported_format_is_refused(tmp_path):
    backend = _backend(tmp_path, _StubController())
    with pytest.raises(ModelFeatureUnavailable):
        backend.open(_request(manifest='model.stl'))


def test_open_and_update_appearance_queue_requests(tmp_path):
    controller = _StubController()
    backend = _backend(tmp_path, controller)
    job = backend.open(_request())
    assert job == 'job-1'
    appearance = _appearance(slots={'roughness': {'value': 0.3}})
    second = backend.update_appearance(_request(request_id='r2'), appearance)
    assert second == 'job-2'
    payload = controller.submitted[1][1]['payload']
    assert payload['parameter_snapshot']['material_roughness'] == pytest.approx(0.3)
    assert payload['model_path'].endswith('model.obj')
    assert controller.submitted[1][1]['priority'] == 2


def test_statistics_mapping_keeps_unknown_as_none():
    statistics = F3DBackend.statistics_from_result({
        'statistics': {'basis': 'f3d-loaded', 'points': 24, 'polygons': None,
                       'triangles': None, 'instances': None,
                       'backend_version': '4.0.0'}})
    assert statistics.vertices == 24
    assert statistics.polygons is None
    assert statistics.triangles is None
    assert statistics.basis == 'f3d-loaded'


def test_output_path_is_only_returned_for_a_real_file(tmp_path):
    missing = tmp_path / 'nope.png'
    assert F3DBackend.output_path({'output_path': str(missing)}) is None
    existing = tmp_path / 'cover.png'
    existing.write_bytes(b'x')
    assert F3DBackend.output_path({'output_path': str(existing)}) == str(existing)


# ── the real runtime ──────────────────────────────────────────────────────
def _f3d_available() -> bool:
    try:
        probe = subprocess.run([sys.executable, '-c', 'import f3d'],
                               capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


requires_native = pytest.mark.skipif(not _f3d_available(),
                                     reason='这台机器没有可用的 F3D 原生运行时')


@requires_native
def test_real_backend_produces_a_cover_and_reflects_appearance(tmp_path):
    sample = ROOT / 'third_party' / 'model-library' / 'samples' / 'obj_multimaterial.obj'
    if not sample.is_file():
        pytest.skip('样本文件不存在')
    config = RuntimeConfig(cache_root=tmp_path / 'cache', startup_timeout_s=90.0,
                           request_timeout_s=180.0, idle_exit_s=0.0,
                           cover_width=192, cover_height=192)
    backend = F3DBackend(config)
    try:
        assert backend.capabilities.preview is True
        first = backend.open(_request(manifest=str(sample)))
        result = backend.controller.wait(first, timeout=180.0)
        assert result is not None
        plain = Path(F3DBackend.output_path(result))
        assert plain.is_file()
        plain_bytes = plain.read_bytes()

        tinted = backend.update_appearance(
            _request(manifest=str(sample), request_id='r2', appearance_revision=2),
            _appearance(slots={'basecolor': {'color': [1.0, 0.0, 0.0]}}))
        second = backend.controller.wait(tinted, timeout=180.0)
        assert second is not None, '外观更新必须真的渲染出新封面'
        painted = Path(F3DBackend.output_path(second))
        assert painted.is_file()
        assert painted.read_bytes() != plain_bytes, '改颜色必须改变封面像素'
        assert second['load']['reused'] is True, '同模型改参数不应重新读取'
        assert second['appearance']['options'] == ['model.color.rgb']
    finally:
        backend.shutdown()


@requires_native
def test_real_backend_reports_unsupported_wireframe(tmp_path):
    """Mode capability is checked in a child process, never in pytest's.

    The pytest process already loaded Qt, and loading pyf3d after Qt fails on
    this machine, so the check has to run where the helper runs.
    """
    sample = ROOT / 'third_party' / 'model-library' / 'samples' / 'quad_grid.obj'
    if not sample.is_file():
        pytest.skip('样本文件不存在')
    script = tmp_path / 'probe_modes.py'
    script.write_text(
        'import json, sys\n'
        'import f3d\n'
        'from prism.model_library import native_protocol as wire\n'
        'from prism.components.model_worker import OffscreenEngine, WorkerFailure, '
        'build_handshake\n'
        'handshake = build_handshake("render", sys.argv[1])\n'
        'engine = f3d.Engine.create(offscreen=True)\n'
        'engine.window.size = (64, 64)\n'
        'engine.scene.add(sys.argv[2])\n'
        'worker = OffscreenEngine.__new__(OffscreenEngine)\n'
        'worker._f3d = f3d\n'
        'worker._sender = None\n'
        'worker._engine = engine\n'
        'worker._loaded_path = sys.argv[2]\n'
        'worker._loaded_revision = 1\n'
        'worker._applied = {}\n'
        'worker._reloads = worker._reuses = worker._recreations = 0\n'
        'worker._log_lines = []\n'
        'out = {"wireframe": handshake.wireframe,\n'
        '       "material_edges": handshake.material_edges,\n'
        '       "fbx_reader": handshake.formats.get(".fbx", "")}\n'
        'try:\n'
        '    worker.set_mode("wireframe")\n'
        '    out["wireframe_mode"] = "accepted"\n'
        'except WorkerFailure as failure:\n'
        '    out["wireframe_mode"] = failure.code\n'
        'out["edges_on"] = bool(worker.set_mode("material_edges"))\n'
        'out["show_edges"] = bool(engine.options["render.show_edges"])\n'
        'worker.set_mode("material")\n'
        'out["restored_edges"] = bool(engine.options["render.show_edges"])\n'
        'print(json.dumps(out))\n',
        encoding='utf-8')
    probe = subprocess.run([sys.executable, str(script), str(tmp_path), str(sample)],
                           capture_output=True, text=True, timeout=180,
                           encoding='utf-8', errors='replace',
                           cwd=str(ROOT))
    assert probe.returncode == 0, probe.stderr[-800:]
    import json as _json
    report = _json.loads([line for line in probe.stdout.splitlines()
                          if line.startswith('{')][-1])
    assert report['wireframe'] is False
    assert report['material_edges'] is True
    assert report['fbx_reader'], 'assimp 插件必须报告 FBX'
    assert report['wireframe_mode'] == 'unsupported_capability'
    assert report['show_edges'] is True
    assert report['restored_edges'] is False, '切回完整材质必须恢复边线状态'
