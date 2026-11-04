from types import SimpleNamespace
from prism.selection_policy import plan_selection_delete


def test_delete_plan_preserves_partition_and_does_not_mutate_selection():
    asset = SimpleNamespace(TYPE='pixmap')
    backed = SimpleNamespace(TYPE='custom', _asset_source='original')
    note = SimpleNamespace(TYPE='text')
    selected = [note, asset, backed]
    plan = plan_selection_delete(iter(selected), {'pixmap', 'video'})
    assert plan.assets == (asset, backed)
    assert plan.annotations == (note,)
    assert selected == [note, asset, backed]
    assert not plan.empty
    assert plan_selection_delete([], set()).empty
