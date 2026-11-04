"""GUI-captured board data; worker IO does not read later editor revisions.

This covers board metadata and attachment membership, not canvas geometry.
Canvas item/source acknowledgement still needs its own snapshot protocol.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace

from prism.fileio.sliced_copy import sliced_copy
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
        """Synchronously consume capture_steps, retaining the existing API."""
        steps = cls.capture_steps(scene)
        while True:
            try:
                next(steps)
            except StopIteration as finished:
                return finished.value

    @classmethod
    def capture_steps(cls, scene, *, defer_tag_system=False):
        """Yield None checkpoints and return a BoardSaveSnapshot.

        Use ``snapshot = yield from BoardSaveSnapshot.capture_steps(scene)``
        or consume with next() and read StopIteration.value. Advance on the
        GUI thread: tag-system readback and attachment snapshots still touch
        scene-owned state. The caller controls scheduling and must keep the
        source revision stable throughout capture.
        """
        defaults = dict(note_html='', mindmap_tree=None, workspace_pages=[],
                        workspace_nodes=[], material_records=[], model_records=[],
                        material_library_state={},
                        category_names=[], tag_names=[], library_queries=[])
        # One copy memo for the board: page/document structures can reference
        # the same nested metadata. Separate copies revisited that entire body
        # for every field. The result is still detached from all live values.
        source = {key: getattr(scene, key, value) for key, value in defaults.items()}
        data = yield from sliced_copy(source)
        data['project_id'] = scene.project_id
        if defer_tag_system:
            system = getattr(scene, 'tag_system', None)
            if isinstance(system, TagSystem):
                data['_save_tag_mappings'] = yield from sliced_copy(
                    (system._siblings, system._parents))
            else:
                data['tag_system'] = yield from sliced_copy(system)
        else:
            raw_tags = tag_system(scene).to_dict()
            copied_tags = yield from sliced_copy(raw_tags)
            data['tag_system'] = TagSystem.from_dict(copied_tags)
        yield None
        origin = getattr(scene, 'attachments', None)
        if hasattr(origin, 'snapshot_for_save'):
            attachments = origin.snapshot_for_save()
            # Keep entry identities: saved_to uses them to retain newer edits.
            version_items = []
            for key in attachments._dirty:
                if key in attachments._cache:
                    version_items.append((key, attachments._cache[key]))
                yield None
            versions = tuple(version_items)
        else:
            attachments = (yield from sliced_copy(origin)) if origin is not None else {}
            versions = ()
        data['attachments'] = attachments
        yield None
        return cls(SimpleNamespace(**data), origin, versions)

    def acknowledge(self, scene, filename):
        """Commit source rebinding on the GUI thread, retaining newer edits."""
        if (scene.project_id == self.state.project_id
                and getattr(scene, 'attachments', None) is self._attachment_origin
                and hasattr(self._attachment_origin, 'saved_to')):
            self._attachment_origin.saved_to(filename, self._attachment_versions)
