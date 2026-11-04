import json
import sqlite3
from dataclasses import replace
from copy import deepcopy
import pytest
from prism.model_library.multimaterial.contracts import MaterialTarget, MaterialState, MaterialEditRequest, MaterialOperation
from prism.model_library.multimaterial.resource_plan import ResourcePlan
from prism.model_library.multimaterial.commit_transaction import write_material_commit


def test_transaction_does_not_commit_and_preserves_unknown_fields(tmp_path):
    path = tmp_path / 'transaction.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE model_records(id TEXT PRIMARY KEY, payload TEXT)')
        db.execute('INSERT INTO model_records VALUES(?,?)', ('m', json.dumps({'record_revision': 2, 'future': {'keep': 9}})))
    before_target = MaterialTarget('s', 1, 'm', 1, 1, 0)
    state = MaterialState(replace(before_target, state_revision=1), (), ())
    block = {'future_material': [1, 2]}
    original = deepcopy(block)
    args = dict(model_id='m', expected_revision=2, previous_block=block, state=state,
        initial_state=state, snapshots={}, resources={}, resource_plan=ResourcePlan((), ()),
        stored=[], add_catalog=False, request=MaterialEditRequest('r', 'g', before_target, MaterialOperation.PATCH), signature='sig')
    db = sqlite3.connect(path)
    try:
        db.execute('BEGIN')
        result = write_material_commit(db, **args)
        assert result.after['future'] == {'keep': 9}
        assert result.after['multimaterial']['future_material'] == [1, 2]
        assert result.after['record_revision'] == 3
        assert block == original
        with sqlite3.connect(path) as observer:
            assert json.loads(observer.execute('SELECT payload FROM model_records').fetchone()[0])['record_revision'] == 2
        db.rollback()  # Fault after UPDATE: resource owner must be able to undo it.
        assert json.loads(db.execute('SELECT payload FROM model_records').fetchone()[0])['record_revision'] == 2
        with pytest.raises(ValueError, match='record changed'):
            write_material_commit(db, **dict(args, expected_revision=1))
        with pytest.raises(ValueError, match='batch size'):
            write_material_commit(db, **dict(args, add_catalog=True))
        assert json.loads(db.execute('SELECT payload FROM model_records').fetchone()[0])['record_revision'] == 2
    finally:
        db.close()
