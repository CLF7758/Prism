"""C2: the model selection and the query identity it was made under.

These checks are Qt-free on purpose. The rules they pin down are business
rules, not widget behaviour:

* a pick belongs to the user, not to the page it was made on;
* a pick belongs to one project session and dies with it;
* an export covers a *frozen* scope, so a list refresh cannot change it.
"""
from prism.model_library.selection import (
    ModelSelection, QueryIdentity, SelectionSnapshot)


def test_a_pick_survives_a_page_change_and_a_new_filter():
    selection = ModelSelection('project#1')
    selection.set_single('model-a')
    selection.toggle('model-b')

    # The user pages on and narrows the filter. Neither may drop a pick.
    selection.set_query(QueryIdentity(text='chair', revision=4))
    selection.toggle('model-c')

    assert selection.ids() == ('model-a', 'model-b', 'model-c')
    assert selection.count == 3


def test_only_the_visible_part_of_the_selection_is_mirrored_on_a_page():
    selection = ModelSelection('project#1')
    selection.add_many(['model-a', 'model-b', 'model-c'])

    # Page 2 only shows b and c; the card surface can paint those two and no
    # more. The controller still owns all three.
    assert selection.visible_subset(['model-b', 'model-c', 'model-d']) == (
        'model-b', 'model-c')
    assert selection.count == 3


def test_a_new_project_session_clears_the_selection():
    selection = ModelSelection('project#1')
    selection.set_single('model-a')

    assert selection.bind_scope('project#1') is False
    assert selection.count == 1, '同一会话重复绑定不能清掉用户的批量选择'

    assert selection.bind_scope('project#2') is True
    assert selection.count == 0
    assert selection.current is None


def test_rebinding_the_same_project_after_reopen_clears_the_selection():
    """Reopening the same project is a new session, not the same one."""
    selection = ModelSelection('project#1')
    selection.set_single('model-a')

    assert selection.bind_scope('project#7') is True
    assert selection.count == 0


def test_the_export_scope_is_frozen_when_the_job_starts():
    selection = ModelSelection('project#1')
    selection.set_query(QueryIdentity(text='', sort='name', revision=3))
    selection.add_many(['model-a', 'model-b'])

    snapshot = selection.freeze()
    assert isinstance(snapshot, SelectionSnapshot)
    assert snapshot.model_ids == ('model-a', 'model-b')

    # The user keeps clicking while the export runs.
    selection.toggle('model-c')
    selection.clear()

    assert snapshot.model_ids == ('model-a', 'model-b'), (
        '运行中的导出不能被后续点击改变覆盖范围')
    assert snapshot.scope_token == 'project#1'
    assert snapshot.query.revision == 3
    assert '2 个模型' in snapshot.describe()


def test_an_empty_snapshot_is_false_and_a_full_one_is_true():
    selection = ModelSelection('project#1')
    assert not selection.freeze()
    selection.set_single('model-a')
    assert selection.freeze()
    assert len(selection.freeze()) == 1


def test_toggling_off_moves_the_focus_but_keeps_the_rest():
    selection = ModelSelection('project#1')
    selection.set_single('model-a')
    selection.toggle('model-b')
    assert selection.current == 'model-b'

    assert selection.toggle('model-b') is False
    assert selection.ids() == ('model-a',)
    assert selection.current == 'model-a'


def test_reconcile_only_drops_ids_when_the_full_set_is_known():
    selection = ModelSelection('project#1')
    selection.add_many(['model-a', 'model-b'])

    assert selection.reconcile(['model-a', 'model-b', 'model-c']) == ()
    assert selection.count == 2

    assert selection.reconcile(['model-b']) == ('model-a',)
    assert selection.ids() == ('model-b',)


def test_selection_order_is_stable_so_an_export_is_reproducible():
    selection = ModelSelection('project#1')
    for model_id in ('m3', 'm1', 'm2'):
        selection.toggle(model_id)
    assert selection.ids() == ('m3', 'm1', 'm2')
    assert selection.ids() == selection.ids()


def test_query_identity_compares_every_field():
    base = QueryIdentity(text='chair', folder_id='folder-1', sort='name',
                         revision=2)
    assert base.matches(QueryIdentity('chair', 'folder-1', 'name', 2))
    assert not base.matches(base.with_revision(3))
    assert not base.matches(QueryIdentity('chair', 'folder-1', '-name', 2))
    assert not base.matches(QueryIdentity('chair', 'folder-2', 'name', 2))
    assert not base.matches(None)
    assert base.signature() == ('chair', 'folder-1', 'name', 2)


def test_setting_the_query_identity_never_clears_the_selection():
    selection = ModelSelection('project#1')
    selection.set_single('model-a')
    selection.set_query(QueryIdentity(text='desk', revision=9))
    assert selection.count == 1
    assert selection.query.text == 'desk'


def test_a_pick_does_not_depend_on_the_folder_it_was_made_in():
    selection = ModelSelection('project#1')
    selection.set_query(QueryIdentity(folder_id='folder-1', revision=1))
    selection.set_single('model-a')
    selection.set_query(QueryIdentity(folder_id='folder-2', revision=2))
    assert selection.contains('model-a')


def test_invalid_ids_are_ignored_rather_than_stored():
    selection = ModelSelection('project#1')
    selection.add_many(['', None, 'model-a'])
    assert selection.ids() == ('model-a',)
