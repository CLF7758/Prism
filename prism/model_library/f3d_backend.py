"""Legacy source-tool adapter; application composition uses BabylonBackend."""
from .render_backend import ModelRenderBackend, VERIFIED_MODES, MODE_REASONS


class F3DBackend(ModelRenderBackend):
    """Compatibility entry for historical F3D verification tools."""
