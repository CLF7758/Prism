"""The formal cooperative GUI capture must include global metadata too."""
import json

from tests.test_save_preparation import project, save_project, begin, settled
from tests.test_partial_project_gui import open_canvas_a, rows
from prism.tags import TagSystem


def test_partial_gui_worker_persists_all_global_domains(save_project, qtbot):
    p = save_project
    session = open_canvas_a(p)
    p.scene.tag_names = ['alpha', 'beta']
    p.scene.category_names = ['category']
    p.scene.library_queries = [{'id': 'query-1', 'name': 'saved query', 'scope': 'all'}]
    p.scene.tag_system = TagSystem([('alias', 'alpha')], [('alpha', 'parent')])
    target = p.source.with_name('global-metadata.prism')
    before = {table: rows(p.source, table) for table in
              ('items', 'sqlar', 'attachments', 'future_extension')}
    operation, worker = begin(p, target)
    settled(p, worker, qtbot)
    assert session.acknowledged is not None
    values = dict(rows(target, 'prism_metadata'))
    assert json.loads(values['tags']) == ['alpha', 'beta']
    assert json.loads(values['categories']) == ['category']
    assert json.loads(values['library_queries']) == p.scene.library_queries
    assert json.loads(values['tag_system']) == p.scene.tag_system.to_dict()
    for table, expected in before.items():
        assert rows(target, table) == expected
    assert session.capture(p.scene).changes == ()
