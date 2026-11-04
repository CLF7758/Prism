from dataclasses import replace
import json
import pytest
from prism.model_library.multimaterial.contracts import MaterialTarget, MaterialState, WorkingMaterial
from prism.model_library.multimaterial.history import load_history, append_history, move_history


def state(count=1):
    return MaterialState(MaterialTarget('s',1,'m',1,1,1),
        tuple(WorkingMaterial(str(i),str(i),'name') for i in range(count)),())


def test_sparse_history_and_branch_and_conflict():
    before=state(1000)
    after=replace(before,materials=(replace(before.materials[0],display_name='new'),)+before.materials[1:])
    history=append_history(load_history({},before.target),before,after,'one-gesture')
    assert len(history['undo'][0]['changes']['materials'])==1
    assert len(json.dumps(history))<1000
    undone, shifted=move_history(history,after,'undo')
    assert undone==before
    restored, again=move_history(shifted,undone,'redo')
    assert restored==after
    assert history['redo']==[]
    assert len(again['undo'])==1
    with pytest.raises(ValueError,match='conflicts'):
        move_history(history,before,'undo')
    branch=append_history(shifted,before,after,'new-edit')
    assert branch['redo']==[]
    with pytest.raises(ValueError,match='source/catalog'):
        load_history({'history':history},replace(before.target,source_revision=2))


def test_unknown_fields_preserved_and_history_bounded():
    before=replace(state(),unknown_json='{"future":{"a":1}}')
    current=before
    history=load_history({},before.target)
    for i in range(105):
        after=replace(current,materials=(replace(current.materials[0],display_name=str(i)),))
        history=append_history(history,current,after,str(i))
        current=after
    assert len(history['undo'])==100
    undone,_=move_history(history,current,'undo')
    assert json.loads(undone.unknown_json)==json.loads(before.unknown_json)


def test_removed_rows_restore_exact_order_and_source_reset_is_explicit():
    before=state(5)
    after=replace(before,materials=(before.materials[0],before.materials[4]))
    history=append_history(load_history({},before.target),before,after,'remove')
    restored,_=move_history(history,after,'undo')
    assert restored==before
    changed=replace(before.target,source_revision=2)
    fresh=load_history({'history':history},changed,reset_source=True)
    assert fresh['undo']==[] and fresh['scope']==[2,1]
