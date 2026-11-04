"""Selection classification without widgets, scene mutation or undo ownership."""
from dataclasses import dataclass

@dataclass(frozen=True)
class DeleteSelectionPlan:
    assets: tuple
    annotations: tuple

    @property
    def empty(self):
        return not self.assets and not self.annotations


def plan_selection_delete(selected, trash_types):
    """Capture the selection once; source-backed items go to recoverable trash."""
    assets, annotations = [], []
    for item in selected:
        target = assets if (getattr(item, 'TYPE', '') in trash_types or
                            getattr(item, '_asset_source', None)) else annotations
        target.append(item)
    return DeleteSelectionPlan(tuple(assets), tuple(annotations))
