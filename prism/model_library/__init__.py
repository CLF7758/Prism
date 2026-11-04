"""Model-library boundaries. No F3D import, disk access or worker startup here.

Start implementation with docs/MODEL_LIBRARY_HANDOFF.md.
"""

from .contracts import ModelFeatureUnavailable
from .service import ModelLibraryService

__all__ = ["ModelFeatureUnavailable", "ModelLibraryService"]
