"""Renderer-independent model preview port and controller lifecycle.

Native engines stay in helper processes. Legacy whole-model appearance
snapshots are interpreted only at the compatibility boundary.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from . import native_protocol as wire
from .contracts import (
    ModelCapabilities, ModelFeatureUnavailable, ModelJobKind, ModelJobRequest,
    ModelStatistics,
)
from .native_jobs import NativeModelJobController, RuntimeConfig

#: The view modes the F3D backend can draw today.
VERIFIED_MODES = ('material', 'material_edges')

#: Why a mode is not offered, so the UI can show the reason instead of a dead
#: button.
MODE_REASONS = {
    'wireframe': ('F3D 4.0.0 的公共接口只提供边线叠加（SetEdgeVisibility），'
                  '没有纯线框表示；未打补丁前该模式保持禁用'),
}


class ModelRenderBackend:
    """``ModelPreviewPort`` implementation backed by measured native facts."""

    def __init__(self, runtime_config: RuntimeConfig,
                 controller: Optional[NativeModelJobController] = None):
        self._config = runtime_config
        self._controller = controller or NativeModelJobController(runtime_config)
        self._capabilities: Optional[ModelCapabilities] = None
        self._probe: Optional[Mapping[str, Any]] = None

    # ------------------------------------------------------------------
    # Port
    # ------------------------------------------------------------------
    @property
    def controller(self) -> NativeModelJobController:
        return self._controller

    @property
    def capabilities(self) -> ModelCapabilities:
        if self._capabilities is None:
            self._capabilities = self._measure_capabilities()
        return self._capabilities

    def refresh_capabilities(self) -> ModelCapabilities:
        """Re-run the handshake after a runtime change (never guesses)."""
        self._capabilities = None
        self._probe = None
        return self.capabilities

    @property
    def capability_report(self) -> Mapping[str, Any]:
        """The measured facts behind :attr:`capabilities`, for logs and reports."""
        if self._probe is None:
            self._probe = self._controller.probe()
        return dict(self._probe)

    def open(self, request: ModelJobRequest) -> str:
        """Queue a cover/inspect job for one model. Returns the job id."""
        self._require_runtime()
        self._require_supported(request)
        return self._controller.submit(
            request, kind=str(getattr(request.kind, 'value', request.kind)),
            payload=self._payload(request), priority=3)

    def update_appearance(self, request: ModelJobRequest, appearance) -> str:
        """Queue a rerender with a new appearance snapshot.

        The appearance is snapshotted into derived preview maps *before* the
        request leaves the main process, so the helper never reads project
        data and a later edit cannot change a running job.
        """
        self._require_runtime()
        self._require_supported(request)
        snapshot = self.parameter_snapshot(request, appearance)
        return self._controller.submit(
            request, kind='preview',
            payload=self._payload(request, snapshot=snapshot), priority=2)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def parameter_snapshot(self, request: ModelJobRequest, appearance,
                           read_texture=None) -> dict:
        """Build and return the JSON-safe parameter snapshot for one model."""
        from . import f3d_parameters

        workdir = _snapshot_dir(self._config, request)
        snapshot = f3d_parameters.build_snapshot(appearance, read_texture, workdir)
        return snapshot.to_payload()

    @staticmethod
    def statistics_from_result(result: Mapping[str, Any]) -> ModelStatistics:
        """Map the helper's statistics report onto the shared contract."""
        report = dict(result.get('statistics') or {})
        basis = str(report.get('basis') or 'unknown')
        return ModelStatistics(
            vertices=_as_int(report.get('points')),
            triangles=_as_int(report.get('triangles')),
            polygons=_as_int(report.get('polygons')),
            instances=_as_int(report.get('instances')),
            basis=basis,
            backend_version=str(report.get('backend_version') or
                                report.get('vtk_version') or ''),
        )

    @staticmethod
    def output_path(result: Mapping[str, Any]) -> Optional[str]:
        """The published cover path of a completed job, never a partial one."""
        path = result.get('output_path')
        if path and Path(str(path)).is_file():
            return str(path)
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _measure_capabilities(self) -> ModelCapabilities:
        probe = self._controller.probe(timeout=self._config.startup_timeout_s)
        self._probe = probe
        if not probe.get('available'):
            # Fail fast and visibly: no preview, no silent fallback to a fake
            # renderer, and the reason is part of the report.
            return ModelCapabilities(backend_version='')
        handshake: wire.Handshake = probe['handshake']
        formats = dict(handshake.formats)
        readable = all(formats.get(ext) for ext in wire.SUPPORTED_FORMATS)
        return ModelCapabilities(
            browse=False,
            import_models=False,
            export_originals=False,
            edit_metadata=False,
            edit_appearance=bool(handshake.appearance),
            preview=bool(readable),
            wireframe=bool(handshake.wireframe),
            backend_version=str(handshake.backend_version),
        )

    def _require_runtime(self) -> None:
        if not self.capabilities.preview:
            reason = str((self.capability_report or {}).get('reason') or '')
            raise ModelFeatureUnavailable(
                'F3D 原生运行时不可用：%s' % (reason or '握手未通过'))

    def _require_supported(self, request: ModelJobRequest) -> None:
        reference = str(getattr(request, 'manifest_ref', '') or '')
        suffix = Path(reference).suffix.lower()
        if suffix and suffix not in wire.SUPPORTED_FORMATS:
            raise ModelFeatureUnavailable('不支持的模型格式：%s' % suffix)

    def _payload(self, request: ModelJobRequest, *,
                 snapshot: Optional[Mapping[str, Any]] = None) -> dict:
        manifest = str(getattr(request, 'manifest_ref', '') or '')
        payload = {
            'model_path': manifest,
            'source_revision': int(getattr(request.identity, 'source_revision', 0) or 0),
            'model_id': str(getattr(request.identity, 'model_id', '') or ''),
            'mode': 'material',
            'width': int(self._config.cover_width),
            'height': int(self._config.cover_height),
            'statistics': True,
            'environment_revision': 2,
        }
        if snapshot is not None:
            payload['parameter_snapshot'] = dict(snapshot)
        else:
            empty = _empty_snapshot()
            payload['parameter_snapshot'] = empty
        return payload

    # ------------------------------------------------------------------
    # Lifecycle passthroughs
    # ------------------------------------------------------------------
    def cancel(self, job_id: str) -> None:
        self._controller.cancel(job_id)

    def close_project(self, project_session: str) -> None:
        self._controller.close_project(project_session)

    def shutdown(self) -> None:
        self._controller.shutdown()

    def describe(self) -> Mapping[str, Any]:
        """One-call summary for diagnostics and the handoff report."""
        report = self.capability_report
        handshake = report.get('handshake')
        return {
            'backend_version': self.capabilities.backend_version,
            'available': bool(report.get('available')),
            'reason': str(report.get('reason') or ''),
            'formats': dict(getattr(handshake, 'formats', {}) or {}),
            'statistics': str(getattr(handshake, 'statistics', 'none')),
            'wireframe': bool(self.capabilities.wireframe),
            'verified_modes': list(VERIFIED_MODES),
            'mode_reasons': dict(MODE_REASONS),
            'worker_pid': int(report.get('worker_pid') or 0),
            'restarts': int(report.get('restarts') or 0),
        }


def _empty_snapshot() -> dict:
    from .f3d_parameters import ParameterSnapshot
    return ParameterSnapshot().to_payload()


def _snapshot_dir(config: RuntimeConfig, request: ModelJobRequest) -> Path:
    """A derived cache directory that is specific to one model revision."""
    identity = request.identity
    key = hashlib.sha256(('%s|%s|%s' % (
        identity.project_session, identity.model_id, identity.source_revision)
    ).encode('utf-8')).hexdigest()[:16]
    path = Path(config.cache_root) / 'appearance' / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

