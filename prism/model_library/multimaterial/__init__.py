"""Multi-material handoff boundaries. No parser, persistence or renderer here.

Read docs/MODEL_MULTI_MATERIAL_DESIGN_20261027.md before implementing.
The default service is deliberately unavailable; legacy whole-model appearance
continues through prism.model_library.appearance until explicit integration.
"""
from .service import MultiMaterialService

__all__ = ['MultiMaterialService']
