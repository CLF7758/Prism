"""Offscreen native worker: one process, one engine, many covers.

Two modes live here on purpose:

``--mode render``      the F3D offscreen engine that renders covers and reports
                       what it really loaded;
``--mode statistics``  an independent VTK reader that counts the geometry the
                       file declares (triangulated with a documented filter).

They must stay in separate processes: the F3D wheel ships its own VTK build and
loading the standalone ``vtk`` modules into the same process is not supported
(it fails fast). The statistics job therefore runs the same module with a
different mode and never imports F3D.

Nothing in this module is imported by Prism itself: it is the helper's entry
point, started by :class:`prism.model_library.native_jobs.NativeModelJobController`.
Protocol frames go over the socket; F3D's own progress output goes to stdout,
which the controller redirects to a bounded log file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from prism.model_library import native_protocol as wire


#: Environment the covers and the interactive view share. A change here must
#: bump :data:`ENVIRONMENT_REVISION` so a cached cover cannot survive it.
ENVIRONMENT = {
    'render.background.color': [0.0, 0.0, 0.0],
    'render.effect.antialiasing.mode': 'fxaa',
    'render.effect.tone_mapping': False,
    'render.effect.ambient_occlusion': False,
    'render.grid.enable': False,
    'render.axes_grid.enable': False,
    'model.point_sprites.size': 6.0,
    'render.line_width': 1.0,
}
ENVIRONMENT_REVISION = 2

#: ``ModelViewMode`` values this worker can draw.
MODE_MATERIAL = 'material'
MODE_MATERIAL_EDGES = 'material_edges'
MODE_WIREFRAME = 'wireframe'
VIEW_MODES = (MODE_MATERIAL, MODE_MATERIAL_EDGES, MODE_WIREFRAME)

#: Formats the offscreen engine is expected to read without a conversion step.
FORMAT_READERS = {'.fbx': 'assimp', '.obj': 'OBJ', '.glb': 'GLB', '.gltf': 'GLTF'}


# ── shared plumbing ───────────────────────────────────────────────────────
class WorkerFailure(RuntimeError):
    """A request that cannot be served, with the code the controller reports."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


def _now_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


class _Sender:
    """Bounded, non-blocking log forwarding to the controller."""

    def __init__(self, channel: wire.Channel, *, max_per_second: int = 200):
        self._channel = channel
        self._max = max(1, int(max_per_second))
        self._window_start = time.monotonic()
        self._count = 0
        self._suppressed = 0
        self._lock = __import__('threading').Lock()

    def send(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            try:
                self._channel.send(payload)
            except Exception:  # noqa: BLE001 - the helper must not die on IPC
                pass

    def log(self, level: str, message: str, **fields: Any) -> None:
        text = str(message or '').strip()
        if not text:
            return
        now = time.monotonic()
        if now - self._window_start > 1.0:
            self._window_start = now
            self._count = 0
            if self._suppressed:
                suppressed, self._suppressed = self._suppressed, 0
                self.send(wire.message(wire.LOG, level='debug',
                                       message='（%d 条重复原生日志已合并）' % suppressed))
        self._count += 1
        if self._count > self._max:
            self._suppressed += 1
            return
        payload = wire.message(wire.LOG, level=str(level or 'info'), message=text[:2000])
        if fields:
            payload['fields'] = {k: _json_safe(v) for k, v in fields.items()}
        self.send(payload)

    def event(self, name: str, **fields: Any) -> None:
        self.send(wire.message(wire.EVENT, event=name,
                               fields={k: _json_safe(v) for k, v in fields.items()}))


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


# ── F3D offscreen engine ──────────────────────────────────────────────────
class OffscreenEngine:
    """One long-lived F3D engine that survives model and appearance changes."""

    def __init__(self, sender: _Sender):
        import f3d

        self._f3d = f3d
        self._sender = sender
        self._engine = None
        self._loaded_path = ''
        self._loaded_revision = None
        self._applied: dict = {}
        self._reloads = 0
        self._reuses = 0
        self._recreations = 0
        self._log_lines: list = []

    # -- lifecycle -----------------------------------------------------
    @property
    def engine(self):
        return self._engine

    def _create(self) -> None:
        f3d = self._f3d
        engine = f3d.Engine.create(offscreen=True)
        options = engine.options
        for key, value in ENVIRONMENT.items():
            options[key] = value
        self._engine = engine
        self._loaded_path = ''
        self._loaded_revision = None
        self._applied = {}

    def ensure(self, width: int, height: int) -> None:
        if self._engine is None:
            self._create()
        self._engine.window.size = (max(1, int(width)), max(1, int(height)))

    def close(self) -> None:
        engine, self._engine = self._engine, None
        self._loaded_path = ''
        self._loaded_revision = None
        self._applied = {}
        if engine is not None:
            try:
                engine.scene.clear()
            except Exception:  # noqa: BLE001 - a cleared engine may already be gone
                pass
            del engine

    # -- loading -------------------------------------------------------
    def load(self, path: str, source_revision: Optional[int] = None,
             size: tuple = (512, 512)) -> dict:
        """Load a model, reusing the engine when the file did not change."""
        start = time.perf_counter()
        path = str(Path(path))
        if not Path(path).is_file():
            raise WorkerFailure(wire.ERR_MISSING_DEPENDENCY, '模型主文件不存在：%s' % path)
        if self._engine is None:
            self._create()
        self._engine.window.size = (max(1, int(size[0])), max(1, int(size[1])))
        reuse = (self._loaded_path == path and self._loaded_revision == source_revision
                 and self._engine is not None)
        if reuse:
            self._reuses += 1
            return {'reused': True, 'load_ms': _now_ms(start), 'path': path}
        reload_how = 'first-load'
        if self._loaded_path:
            reload_how = self._reload(path)
        else:
            self._add(path)
        self._loaded_path = path
        self._loaded_revision = source_revision
        # A fresh model must not inherit the previous model's overrides.
        self._apply_options({})
        self._reloads += 1
        return {'reused': False, 'reload': reload_how, 'load_ms': _now_ms(start),
                'path': path}

    def _add(self, path: str) -> None:
        try:
            self._engine.scene.add(path)
        except Exception as error:  # noqa: BLE001 - surfaced as a request failure
            code = wire.ERR_UNSUPPORTED_FORMAT
            text = str(error)
            if 'unsupported content' in text or 'no reader' in text:
                code = wire.ERR_UNSUPPORTED_FORMAT
                text = ('F3D 没有可用于该文件的读取器（ASCII FBX 等内容检查不通过）：%s'
                        % text)
            elif 'Failed to open' in text or 'missing' in text.lower():
                code = wire.ERR_MISSING_DEPENDENCY
            elif 'failed to load scene' in text:
                code = wire.ERR_BAD_FILE
            raise WorkerFailure(code, text) from error

    def _reload(self, path: str) -> str:
        """Clear and reuse the engine; recreate it only when clearing fails."""
        try:
            self._engine.scene.clear()
            self._add(path)
            return 'clear-and-add'
        except WorkerFailure:
            raise
        except Exception:  # noqa: BLE001 - fall back to a fresh engine
            self._recreations += 1
            self.close()
            self._create()
            self._add(path)
            return 'recreated-engine'

    # -- appearance ----------------------------------------------------
    def _apply_options(self, options: Mapping[str, Any]) -> None:
        engine_options = self._engine.options
        for key in list(self._applied):
            if key not in options:
                try:
                    engine_options.remove_value(key)
                except Exception:  # noqa: BLE001 - unset option is already unset
                    pass
        for key, value in options.items():
            engine_options[key] = value
        self._applied = dict(options)

    def apply_snapshot(self, payload: Optional[Mapping[str, Any]]) -> dict:
        from prism.model_library.f3d_parameters import ParameterSnapshot

        snapshot = ParameterSnapshot.from_payload(payload)
        if snapshot.schema != 1:
            raise WorkerFailure(wire.ERR_BAD_PROTOCOL,
                                '参数快照版本不支持：%s' % snapshot.schema)
        self._apply_options(snapshot.option_values())
        return {'options': sorted(self._applied), 'signature': snapshot.signature,
                'translucent': snapshot.translucent,
                'warnings': list(snapshot.warnings),
                'unsupported': list(snapshot.unsupported)}

    # -- rendering -----------------------------------------------------
    def set_mode(self, mode: str) -> str:
        mode = str(mode or MODE_MATERIAL)
        if mode not in VIEW_MODES:
            raise WorkerFailure(wire.ERR_BAD_PROTOCOL, '未知显示模式：%s' % mode)
        if mode == MODE_WIREFRAME:
            raise WorkerFailure(
                wire.ERR_UNSUPPORTED_CAPABILITY,
                'F3D 4.0.0 的公共接口只有边线叠加（SetEdgeVisibility），'
                '没有纯线框表示；未打补丁前不提供该模式')
        self._engine.options['render.show_edges'] = (mode == MODE_MATERIAL_EDGES)
        return mode

    def render(self, mode: str, width: int, height: int, destination: Path,
               camera: Optional[Mapping[str, Any]] = None) -> dict:
        start = time.perf_counter()
        self.set_mode(mode)
        if camera:
            self.apply_camera(camera)
        self._engine.window.size = (max(1, int(width)), max(1, int(height)))
        image = self._engine.window.render_to_image()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.with_name(destination.name + '.part')
        image.save(str(staged))
        os.replace(str(staged), str(destination))
        data = destination.read_bytes()
        return {
            'path': str(destination),
            'sha256': hashlib.sha256(data).hexdigest(),
            'bytes': len(data),
            'width': int(image.width),
            'height': int(image.height),
            'mode': mode,
            'render_ms': _now_ms(start),
        }

    def apply_camera(self, camera: Mapping[str, Any]) -> None:
        """Apply a stored cover camera, or reset to the framed default."""
        window = self._engine.window
        if str(camera.get('preset') or '') == 'reset' or not camera.get('position'):
            window.camera.reset_to_bounds(float(camera.get('zoom') or 0.9))
            return
        window.camera.position = tuple(float(v) for v in camera['position'])
        window.camera.focal_point = tuple(float(v) for v in camera['focal_point'])
        window.camera.view_up = tuple(float(v) for v in camera['view_up'])
        if camera.get('view_angle'):
            window.camera.view_angle = float(camera['view_angle'])

    def camera_state(self, reset: bool = False) -> dict:
        window = self._engine.window
        if reset:
            window.camera.reset_to_bounds(0.9)
        state = window.camera.state
        return {
            'position': list(state.position),
            'focal_point': list(state.focal_point),
            'view_up': list(state.view_up),
            'view_angle': float(state.view_angle),
        }

    # -- statistics ----------------------------------------------------
    def scene_info(self) -> dict:
        info = self._engine.scene.get_scene_info()
        return {'actors': int(info.number_of_actors),
                'points': int(info.number_of_points),
                'cells': int(info.number_of_cells),
                'files': int(info.number_of_files)}

    def hierarchy(self) -> list:
        return [{'id': int(node.id), 'label': node.label, 'level': int(node.level),
                 'visible': bool(node.visible)} for node in
                self._engine.scene.get_scene_hierarchy()]

    def added_files(self) -> list:
        return [str(path.name) for path in self._engine.scene.get_added_files()]


# ── statistics ------------------------------------------------------------
_ACTOR_RE = {
    'points': re.compile(r'^Number of points:\s*(\d+)$'),
    'polygons': re.compile(r'^Number of polygons:\s*(\d+)$'),
    'lines': re.compile(r'^Number of lines:\s*(\d+)$'),
    'vertices': re.compile(r'^Number of vertices:\s*(\d+)$'),
    'cells': re.compile(r'^Number of cells:\s*(\d+)$'),
}


def parse_actor_blocks(text: str) -> list:
    """Parse the per-actor geometry block F3D prints for a loaded scene.

    The text comes from the pinned backend's own debug output, so the numbers
    describe what F3D really loaded (post transform, post instance expansion).
    It is a *secondary* source: the authoritative counts are what
    :meth:`OffscreenEngine.scene_info` reports, and a format whose reader does
    not print a breakdown reports ``None`` instead of a guess.
    """
    actors = []
    current: Optional[dict] = None
    for raw in str(text).splitlines():
        line = raw.strip()
        match = _ACTOR_RE['points'].match(line)
        if match:
            current = {'points': int(match.group(1)), 'polygons': None,
                       'lines': None, 'vertices': None, 'cells': None}
            continue
        if current is None:
            continue
        for key in ('polygons', 'lines', 'vertices'):
            match = _ACTOR_RE[key].match(line)
            if match:
                current[key] = int(match.group(1))
                break
        else:
            match = _ACTOR_RE['cells'].match(line)
            if match:
                current['cells'] = int(match.group(1))
                actors.append(current)
                current = None
    if current is not None:
        actors.append(current)
    return actors


def summarise_actor_blocks(actors) -> dict:
    if not actors:
        return {}
    def total(key):
        values = [actor.get(key) for actor in actors]
        if any(value is None for value in values):
            return None
        return int(sum(values))
    return {
        'actors_parsed': len(actors),
        'polygons': total('polygons'),
        'line_elements': total('lines'),
        'point_elements': total('vertices'),
        'cells': total('cells'),
        'points': total('points'),
    }


def f3d_statistics(actor_text: str, scene: Mapping[str, Any],
                   reader: str, backend_version: str, vtk_version: str) -> dict:
    """The loaded-scene report: F3D numbers plus the printed per-actor blocks."""
    parsed = summarise_actor_blocks(parse_actor_blocks(actor_text))
    notes = ['points/cells 来自 F3D SceneInfo（加载后的数据，不是 DCC 原始拓扑）']
    if parsed:
        notes.append('逐 Actor 明细来自后端自身输出，已按 Actor 汇总')
    else:
        notes.append('该读取器没有输出逐 Actor 明细：多边形/线/点元素记为未知')
    return {
        'basis': 'f3d-loaded',
        'backend_version': backend_version,
        'vtk_version': vtk_version,
        'reader': reader,
        'actors': scene.get('actors'),
        'points': scene.get('points'),
        'cells': scene.get('cells'),
        'polygons': parsed.get('polygons'),
        'triangles': None,
        'line_elements': parsed.get('line_elements'),
        'point_elements': parsed.get('point_elements'),
        'instances': None,
        'meshes': None,
        'notes': notes,
    }


def _reader_name(path: str) -> str:
    return FORMAT_READERS.get(Path(path).suffix.lower(), 'unknown')


_SCENE_DETAIL_MARKERS = ('Number of points', 'Number of polygons', 'Number of cells',
                         'Number of lines', 'Number of vertices')


def _looks_like_scene_detail(text: str) -> bool:
    return any(marker in text for marker in _SCENE_DETAIL_MARKERS)


# ── render mode ───────────────────────────────────────────────────────────
class RenderWorker:
    def __init__(self, sender: _Sender, cache_root: Path):
        self.sender = sender
        self.cache_root = Path(cache_root)
        self.engine = OffscreenEngine(sender)
        self._actor_log: list = []
        self._flush_log = False

    # -- f3d log capture ----------------------------------------------
    def _on_log(self, level, message) -> None:
        text = str(message or '')
        if not text.strip():
            return
        name = str(getattr(level, 'name', level) or 'info')
        # The scene-graph dump the backend prints per load is useful for the
        # statistics path but far too chatty for the event pipeline.
        if name.upper() == 'DEBUG':
            if self._flush_log and _looks_like_scene_detail(text):
                self._actor_log.append(text)
            return
        self.sender.log(name, text)

    def capture_log(self) -> None:
        import f3d
        f3d.Log.set_verbose_level(f3d.Log.VerboseLevel.INFO)
        f3d.Log.forward(self._on_log)
        try:
            # Verbose level is per-thread state in the native library: after the
            # engine exists it can drop back to DEBUG, so re-apply it here.
            f3d.Log.set_verbose_level(f3d.Log.VerboseLevel.INFO)
        except Exception:  # noqa: BLE001 - verbosity is best effort
            pass

    # -- requests ------------------------------------------------------
    def handle(self, kind: str, identity: Mapping[str, Any],
               payload: Mapping[str, Any]) -> dict:
        start = time.perf_counter()
        render = self.engine
        render.ensure(int(payload.get('width') or 512), int(payload.get('height') or 512))
        model_path = str(payload.get('model_path') or payload.get('manifest_ref') or '')
        if not model_path:
            raise WorkerFailure(wire.ERR_MISSING_DEPENDENCY, '请求缺少模型路径')
        self._flush_log = True
        self._actor_log.clear()
        load = render.load(model_path,
                           payload.get('source_revision'),
                           (payload.get('width') or 512, payload.get('height') or 512))
        applied = render.apply_snapshot(payload.get('parameter_snapshot'))
        statistics = None
        if payload.get('statistics'):
            scene = render.scene_info()
            statistics = f3d_statistics('\n'.join(self._actor_log), scene,
                                       _reader_name(model_path),
                                       _backend_version(), _vtk_version())
        self._flush_log = False
        mode = str(payload.get('mode') or MODE_MATERIAL)
        requested = str(payload.get('output') or '').strip()
        destination = Path(requested) if requested else self._default_output(identity)
        if kind == 'inspect':
            scene = render.scene_info()
            return {
                'outputs': {},
                'load': load,
                'appearance': applied,
                'statistics': statistics or f3d_statistics(
                    '\n'.join(self._actor_log), scene, _reader_name(model_path),
                    _backend_version(), _vtk_version()),
                'scene': scene,
                'camera': render.camera_state(),
                'hierarchy': render.hierarchy(),
                'added_files': render.added_files(),
                'timing': {'total_ms': _now_ms(start), 'load_ms': load.get('load_ms')},
            }
        self.sender.event('rendered', phase='start', job_id=identity.get('request_id'))
        # "render started" and "cover bytes written" are separate facts; the
        # worker knows both and the main process can only see the second.
        self.sender.event('model_cover_render_started',
                          job_id=identity.get('request_id'),
                          mode=str(mode),
                          width=int(payload.get('width') or 512),
                          height=int(payload.get('height') or 512))
        output = render.render(mode,
                               int(payload.get('width') or 512),
                               int(payload.get('height') or 512),
                               destination,
                               payload.get('camera'))
        scene = render.scene_info()
        if statistics is None:
            statistics = f3d_statistics('\n'.join(self._actor_log), scene,
                                        _reader_name(model_path),
                                        _backend_version(), _vtk_version())
        return {
            'outputs': output,
            'load': load,
            'appearance': applied,
            'statistics': statistics,
            'scene': scene,
            'camera': render.camera_state(),
            'reuse': {'reloads': render._reloads, 'reuses': render._reuses,
                      'recreations': render._recreations},
            'environment': {'revision': ENVIRONMENT_REVISION, **ENVIRONMENT},
            'timing': {'total_ms': _now_ms(start), 'load_ms': load.get('load_ms'),
                       'render_ms': output.get('render_ms')},
        }

    def _default_output(self, identity: Mapping[str, Any]) -> Path:
        model = str(identity.get('model_id') or 'model').replace('/', '_')
        return self.cache_root / 'scratch' / ('%s-%s.png' % (
            model, identity.get('request_id') or 'job'))

    def shutdown(self) -> None:
        self.engine.close()


# ── statistics mode (independent VTK) ─────────────────────────────────────
class StatisticsWorker:
    """Counts the geometry a file declares, without F3D and without guessing."""

    def __init__(self, sender: _Sender, cache_root: Path):
        self.sender = sender
        self.cache_root = Path(cache_root)

    def handle(self, kind: str, identity: Mapping[str, Any],
               payload: Mapping[str, Any]) -> dict:
        start = time.perf_counter()
        model_path = str(payload.get('model_path') or payload.get('manifest_ref') or '')
        if not model_path or not Path(model_path).is_file():
            raise WorkerFailure(wire.ERR_MISSING_DEPENDENCY,
                                '模型主文件不存在：%s' % model_path)
        suffix = Path(model_path).suffix.lower()
        statistics = self.count(model_path, suffix)
        return {'outputs': {}, 'statistics': statistics,
                'timing': {'total_ms': _now_ms(start)}}

    def count(self, path: str, suffix: str) -> dict:
        notes = []
        if suffix in ('.obj', '.gltf', '.glb'):
            try:
                return self._vtk_count(path, suffix, notes)
            except ImportError as error:
                notes.append('没有可用的 VTK 读取模块：%s' % error)
            except Exception as error:  # noqa: BLE001 - reported, not raised
                notes.append('独立读取失败：%s' % error)
        else:
            notes.append('没有独立的 FBX 几何读取器：顶点/多边形/三角形记为未知，'
                         '不把 F3D 的 cells 当作三角形')
        return {
            'basis': 'unavailable', 'backend_version': _vtk_version(),
            'vtk_version': _vtk_version(), 'reader': suffix.lstrip('.'),
            'actors': None, 'points': None, 'cells': None, 'polygons': None,
            'triangles': None, 'line_elements': None, 'point_elements': None,
            'instances': None, 'meshes': None, 'notes': notes,
        }

    def _vtk_count(self, path: str, suffix: str, notes: list) -> dict:
        from vtkmodules.vtkFiltersCore import vtkTriangleFilter
        from vtkmodules.vtkIOGeometry import vtkOBJReader

        blocks = []
        if suffix == '.obj':
            reader = vtkOBJReader()
            reader.SetFileName(path)
            reader.Update()
            _collect_polydata(reader.GetOutput(), blocks)
            meshes = 1
            instances = 0
        else:
            from vtkmodules.vtkIOGeometry import vtkGLTFReader
            reader = vtkGLTFReader()
            reader.SetFileName(path)
            reader.Update()
            _collect_polydata(reader.GetOutput(), blocks)
            meshes, instances = _gltf_mesh_inventory(path, notes)
        points = polygons = lines = vertices = strips = triangles = 0
        for block in blocks:
            points += block.GetNumberOfPoints()
            polygons += block.GetNumberOfPolys()
            lines += block.GetNumberOfLines()
            vertices += block.GetNumberOfVerts()
            strips += block.GetNumberOfStrips()
            filter_ = vtkTriangleFilter()
            filter_.SetInputData(block)
            filter_.Update()
            triangles += filter_.GetOutput().GetNumberOfPolys()
            filter_ = None
        if strips:
            notes.append('三角带已计入三角形总数（%d 条）' % strips)
        notes.append('三角化路径：vtkTriangleFilter，独立于 F3D 的加载结果')
        notes.append('数量是文件声明的几何（basis=file），不是 DCC 原始拓扑')
        return {
            'basis': 'file+vtk-triangulation',
            'backend_version': _vtk_version(),
            'vtk_version': _vtk_version(),
            'reader': vtkOBJReader.__name__ if suffix == '.obj' else 'vtkGLTFReader',
            'actors': len([b for b in blocks if b is not None]),
            'points': int(points), 'cells': int(polygons + lines + vertices + strips),
            'polygons': int(polygons + strips), 'triangles': int(triangles),
            'line_elements': int(lines), 'point_elements': int(vertices),
            'instances': instances, 'meshes': meshes, 'notes': notes,
        }

    def shutdown(self) -> None:
        return None


def _collect_polydata(source, sink: list, depth: int = 0) -> None:
    """Flatten a reader's output into the polygonal datasets inside it.

    ``vtkGLTFReader`` returns a (possibly nested) ``vtkMultiBlockDataSet`` while
    ``vtkOBJReader`` returns ``vtkPolyData``. Anything that is neither is skipped
    instead of being treated as geometry.
    """
    if source is None or depth > 4:
        return
    if hasattr(source, 'GetNumberOfPolys') and hasattr(source, 'GetNumberOfLines'):
        if source.GetNumberOfPoints() or source.GetNumberOfCells():
            sink.append(source)
        return
    if hasattr(source, 'GetNumberOfBlocks'):
        for index in range(source.GetNumberOfBlocks()):
            _collect_polydata(source.GetBlock(index), sink, depth + 1)


def _gltf_mesh_inventory(path: str, notes: list):
    """Count meshes and instance nodes straight from the glTF JSON."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        notes.append('无法读取文件以统计实例：%s' % error)
        return None, None
    if raw[:4] == b'glTF':
        return _inventory_from_glb(raw, notes)
    try:
        document = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        notes.append('无法解析 glTF JSON 以统计实例：%s' % error)
        return None, None
    return _inventory_from_document(document)


def _inventory_from_glb(raw: bytes, notes: list):
    import struct

    try:
        offset = 12
        while offset + 8 <= len(raw):
            length, kind = struct.unpack_from('<II', raw, offset)
            if kind == 0x4E4F534A:  # 'JSON'
                document = json.loads(raw[offset + 8:offset + 8 + length].decode('utf-8-sig'))
                return _inventory_from_document(document)
            offset += 8 + length
    except Exception as error:  # noqa: BLE001 - reported, not raised
        notes.append('无法解析 GLB 头以统计实例：%s' % error)
    return None, None


def _inventory_from_document(document: Mapping[str, Any]):
    meshes = len(document.get('meshes') or [])
    nodes = document.get('nodes') or []
    referenced = [node.get('mesh') for node in nodes
                  if isinstance(node, Mapping) and 'mesh' in node]
    instances = max(0, len(referenced) - len(set(referenced)))
    return meshes, instances


# ── version helpers ───────────────────────────────────────────────────────
def _backend_version() -> str:
    try:
        import f3d
        return str(f3d.Engine.get_lib_info().version_full)
    except Exception:  # noqa: BLE001 - reported as unknown, never invented
        return ''


def _vtk_version() -> str:
    try:
        from vtkmodules.vtkCommonCore import vtkVersion
        return str(vtkVersion.GetVTKVersion())
    except Exception:  # noqa: BLE001
        return ''


# ── entry point ───────────────────────────────────────────────────────────
def build_handshake(mode: str, cache_root: Path) -> wire.Handshake:
    features = {'environment_revision': ENVIRONMENT_REVISION,
                'modes': list(VIEW_MODES), 'cover': True, 'camera': True}
    if mode == 'statistics':
        return wire.Handshake(
            worker=wire.WORKER_NAME_STATISTICS,
            backend_version=_vtk_version() or 'vtk-unknown',
            vtk_version=_vtk_version(),
            python_version=platform.python_version(),
            pid=os.getpid(),
            formats={ext: 'vtk' for ext in ('.obj', '.glb', '.gltf')},
            readers=('vtkOBJReader', 'vtkGLTFReader'),
            wireframe=False, material_edges=False, appearance=False,
            statistics='file+vtk-triangulation',
            features={**features, 'fbx_statistics': False},
            limits={'max_frame_bytes': wire.MAX_FRAME_BYTES},
        )
    import f3d

    info = f3d.Engine.get_lib_info()
    readers = []
    formats = {}
    for reader in f3d.Engine.get_readers_info():
        name = str(reader.name)
        readers.append(name)
        for extension in reader.extensions:
            ext = '.' + str(extension).lstrip('.').lower()
            if ext in FORMAT_READERS and ext not in formats:
                formats[ext] = name
    for ext, expected in FORMAT_READERS.items():
        formats.setdefault(ext, '' if ext not in formats else formats[ext])
    return wire.Handshake(
        worker=wire.WORKER_NAME_RENDER,
        protocol_version=wire.NATIVE_PROTOCOL_VERSION,
        backend_version=str(info.version_full),
        vtk_version=str(info.vtk_version),
        python_version=platform.python_version(),
        pid=os.getpid(),
        formats={ext: formats.get(ext, '') for ext in FORMAT_READERS},
        readers=tuple(readers),
        # Measured, not assumed: this build only offers edge overlay.
        wireframe=False,
        material_edges=True,
        appearance=True,
        statistics='f3d-loaded',
        features={'environment_revision': ENVIRONMENT_REVISION,
                  'modes': [MODE_MATERIAL, MODE_MATERIAL_EDGES],
                  'cover': True, 'camera': True,
                  'normal_map_requires_tangents': True,
                  'textures_transform': False,
                  'blending_modes': ['none', 'ddp'],
                  'modules': dict(info.modules)},
        limits={'max_frame_bytes': wire.MAX_FRAME_BYTES,
                'cover_width': 512, 'cover_height': 512},
    )


def serve(mode: str, port: int, token: str, cache_root: Path,
          log_level: str = 'INFO') -> int:
    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    channel = wire.Channel.connect('127.0.0.1', int(port),
                                   timeout=30.0)
    sender = _Sender(channel)
    try:
        handshake = build_handshake(mode, cache_root)
    except Exception as error:  # noqa: BLE001 - a broken runtime must be visible
        channel.send(wire.message(wire.HELLO, token=token,
                                  handshake={'worker': '', 'protocol_version': 0,
                                             'backend_version': '',
                                             'formats': {},
                                             'readers': [], 'error': str(error)}))
        channel.close()
        return 3
    channel.send(wire.message(wire.HELLO, token=token,
                              handshake=handshake.to_payload()))
    worker = (StatisticsWorker(sender, cache_root) if mode == 'statistics'
              else RenderWorker(sender, cache_root))
    if mode != 'statistics':
        worker.capture_log()
        try:
            import f3d
            f3d.Log.set_verbose_level(getattr(f3d.Log.VerboseLevel, log_level.upper()))
        except Exception:  # noqa: BLE001 - logging verbosity is best effort
            pass
    cancelled: set = set()
    try:
        while True:
            try:
                envelope = channel.recv(timeout=0.5)
            except wire.PeerDisconnected:
                # No live peer remains to receive a log/response. Exit with a
                # classified failure; never fabricate a completed render.
                print(json.dumps(dict(event='worker_peer_disconnected', reason='peer_reset')),
                      file=sys.stderr, flush=True)
                return 4
            if envelope is None:
                if channel.closed:
                    return 0
                continue
            try:
                kind = wire.check_message(envelope)
            except wire.ProtocolError as error:
                sender.log('error', str(error))
                continue
            if kind == wire.SHUTDOWN:
                sender.send(wire.message(wire.BYE))
                return 0
            if kind == wire.CANCEL:
                cancelled.add(str(envelope.get('job_id') or ''))
                continue
            if kind == wire.CLOSE_PROJECT:
                cancelled.clear()
                continue
            if kind != wire.REQUEST:
                continue
            job_id = str(envelope.get('job_id') or '')
            identity = envelope.get('identity') or {}
            payload = envelope.get('payload') or {}
            if job_id in cancelled:
                sender.send(wire.message(wire.RESULT, job_id=job_id,
                                         state=wire.STATE_CANCELLED,
                                         identity=identity,
                                         error_code=wire.ERR_CANCELLED))
                cancelled.discard(job_id)
                continue
            started = time.perf_counter()
            try:
                outcome = worker.handle(str(envelope.get('kind') or 'preview'),
                                        identity, payload)
                result = wire.message(wire.RESULT, job_id=job_id,
                                      state=wire.STATE_COMPLETED,
                                      identity=identity, **outcome)
            except WorkerFailure as failure:
                sender.log('error', failure.message, job_id=job_id)
                result = wire.message(wire.RESULT, job_id=job_id,
                                      state=wire.STATE_FAILED, identity=identity,
                                      error_code=failure.code,
                                      error_message=failure.message[:600])
            except MemoryError:
                result = wire.message(wire.RESULT, job_id=job_id,
                                      state=wire.STATE_FAILED, identity=identity,
                                      error_code=wire.ERR_NATIVE_CRASH,
                                      error_message='原生渲染内存不足')
            except Exception as error:  # noqa: BLE001 - reported with the traceback text
                import traceback
                text = traceback.format_exc(limit=6)
                sender.log('error', text, job_id=job_id)
                result = wire.message(wire.RESULT, job_id=job_id,
                                      state=wire.STATE_FAILED, identity=identity,
                                      error_code=wire.ERR_BAD_FILE,
                                      error_message=('%s: %s' % (type(error).__name__,
                                                                 error))[:600])
            result['timing'] = {**dict(result.get('timing') or {}),
                                'worker_total_ms': _now_ms(started)}
            sender.send(result)
            cancelled.discard(job_id)
    finally:
        try:
            worker.shutdown()
        except Exception:  # noqa: BLE001
            pass
        channel.close()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description='Prism 模型库原生辅助进程')
    parser.add_argument('--ipc-port', type=int, required=True)
    parser.add_argument('--ipc-token', required=True)
    parser.add_argument('--cache-root', required=True)
    parser.add_argument('--mode', default='render', choices=('render', 'statistics'))
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:  # noqa: BLE001
                pass
    return serve(args.mode, args.ipc_port, args.ipc_token, Path(args.cache_root),
                 args.log_level)


if __name__ == '__main__':
    sys.exit(main())
