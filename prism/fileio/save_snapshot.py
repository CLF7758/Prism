"""GUI-captured board data; worker IO does not read later editor revisions.

This covers board metadata and attachment membership, not canvas geometry.
Canvas item/source acknowledgement still needs its own snapshot protocol.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace

from prism.performance_trace import measured
from prism.tags import TagSystem, tag_system


@dataclass(frozen=True)
class BoardSaveSnapshot:
    state: object
    _attachment_origin: object = field(repr=False)
    _attachment_versions: tuple = field(repr=False)

    @classmethod
    @measured('project.capture_board_snapshot', 0)
    def capture(cls, scene):
        defaults = dict(note_html='', mindmap_tree=None, workspace_pages=[],
                        workspace_nodes=[], material_records=[], material_library_state={},
                        category_names=[], tag_names=[], library_queries=[])
        data = {key: deepcopy(getattr(scene, key, value)) for key, value in defaults.items()}
        data['project_id'] = scene.project_id
        data['tag_system'] = TagSystem.from_dict(tag_system(scene).to_dict())
        origin = getattr(scene, 'attachments', None)
        if hasattr(origin, 'snapshot_for_save'):
            attachments = origin.snapshot_for_save()
            versions = tuple((key, attachments._cache[key]) for key in attachments._dirty
                             if key in attachments._cache)
        else:
            attachments = deepcopy(origin) if origin is not None else {}
            versions = ()
        data['attachments'] = attachments
        return cls(SimpleNamespace(**data), origin, versions)

    def acknowledge(self, scene, filename):
        """Commit source rebinding on the GUI thread, retaining newer edits."""
        if (scene.project_id == self.state.project_id
                and getattr(scene, 'attachments', None) is self._attachment_origin
                and hasattr(self._attachment_origin, 'saved_to')):
            self._attachment_origin.saved_to(filename, self._attachment_versions)
