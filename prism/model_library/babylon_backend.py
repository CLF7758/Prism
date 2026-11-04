"""Shared model-preview port; all application rendering uses Babylon."""
from .render_backend import ModelRenderBackend
from .contracts import ModelFeatureUnavailable
from prism.material_preview_profile import profile_signature


class BabylonBackend(ModelRenderBackend):
    def _require_runtime(self):
        if not self.capabilities.preview:
            raise ModelFeatureUnavailable('Babylon 模型预览不可用：' + str(self.capability_report.get('reason') or '渲染初始化未通过'))

    def _payload(self, request, *, snapshot=None):
        payload = super()._payload(request, snapshot=snapshot)
        payload['environment_revision'] = profile_signature('model')
        return payload

    def describe(self):
        result = super().describe()
        result.update(verified_modes=['material', 'material_edges', 'wireframe'], mode_reasons={}, renderer='Babylon')
        return result
