"""材质库的软删除：进回收站、恢复、永久删除。

材质的 `deletedAt` 字段本来就在记录里，服务层之前只是没有对外的方法。
这里钉住的是回收站口径：软删之后记录还在工程里，但不再可见；
恢复只碰被软删的那几条；永久删除才真的把记录拿掉。
"""
from datetime import datetime

import pytest

from prism.material_service import iter_visible_materials


def _service(main_window):
    return main_window.view.category_panel.material_service


def _stored(service, record_id):
    """服务真正持有的那一份记录，不是 get() 给的副本。"""
    return next(r for r in service.records if r['id'] == record_id)


def test_trash_marks_the_record_but_keeps_it_in_the_project(main_window):
    service = _service(main_window)
    scene = main_window.view.scene
    record = service.create(name='木头')

    assert service.trash([record['id']]) == {record['id']}

    assert _stored(service, record['id'])['deletedAt'] is not None
    assert len(service.records) == 1
    assert list(iter_visible_materials(scene)) == []
    assert service.list_records() == []


def test_trash_twice_keeps_the_first_timestamp(main_window):
    service = _service(main_window)
    record = service.create(name='木头')

    assert service.trash([record['id']], '2026-10-19T08:00:00') == {record['id']}
    stamp = _stored(service, record['id'])['deletedAt']
    assert stamp == '2026-10-19T08:00:00'

    assert service.trash([record['id']], '2026-11-01T08:00:00') == set()
    assert _stored(service, record['id'])['deletedAt'] == stamp


def test_trash_returns_only_the_records_it_marked(main_window):
    service = _service(main_window)
    first = service.create(name='木头')
    second = service.create(name='金属')

    assert service.trash([first['id']]) == {first['id']}
    assert _stored(service, second['id'])['deletedAt'] is None


def test_trash_uses_the_given_timestamp_verbatim(main_window):
    service = _service(main_window)
    record = service.create(name='木头')

    service.trash([record['id']], '2026-10-19T08:00:00')

    assert _stored(service, record['id'])['deletedAt'] == '2026-10-19T08:00:00'


def test_trash_without_a_timestamp_stamps_now(main_window):
    service = _service(main_window)
    record = service.create(name='木头')

    service.trash([record['id']])

    stamp = _stored(service, record['id'])['deletedAt']
    assert datetime.fromisoformat(stamp).tzinfo is not None


def test_restore_clears_the_timestamp_of_the_given_record_only(main_window):
    service = _service(main_window)
    first = service.create(name='木头')
    second = service.create(name='金属')
    service.trash([first['id'], second['id']])

    assert service.restore([first['id']]) == {first['id']}
    assert _stored(service, first['id'])['deletedAt'] is None
    assert _stored(service, second['id'])['deletedAt'] is not None


def test_restore_skips_records_that_were_never_trashed(main_window):
    service = _service(main_window)
    record = service.create(name='木头')

    assert service.restore([record['id']]) == set()
    assert _stored(service, record['id'])['deletedAt'] is None


def test_purge_removes_the_record_from_the_project(main_window):
    service = _service(main_window)
    scene = main_window.view.scene
    first = service.create(name='木头')
    second = service.create(name='金属')
    service.trash([first['id']])

    assert service.purge([first['id']]) == {first['id']}
    assert [r['id'] for r in service.records] == [second['id']]
    assert service.get(first['id']) is None
    # scene.material_records 和服务持有的是同一个列表：永久删除必须同步过去。
    assert [r['id'] for r in scene.material_records] == [second['id']]


def test_unknown_ids_and_empty_lists_are_skipped(main_window):
    service = _service(main_window)
    record = service.create(name='木头')

    assert service.trash(['not-a-record']) == set()
    assert service.trash([]) == set()
    assert service.restore(['not-a-record']) == set()
    assert service.restore([]) == set()
    assert service.purge(['not-a-record']) == set()
    assert service.purge([]) == set()
    assert len(service.records) == 1
    assert _stored(service, record['id'])['deletedAt'] is None


def test_invalid_arguments_raise_value_error(main_window):
    service = _service(main_window)
    record = service.create(name='木头')

    with pytest.raises(ValueError):
        service.trash(None)
    with pytest.raises(ValueError):
        service.trash('not-a-list')
    with pytest.raises(ValueError):
        service.trash([123])
    with pytest.raises(ValueError):
        service.trash([record['id']], '')
    with pytest.raises(ValueError):
        service.restore(None)
    with pytest.raises(ValueError):
        service.purge(None)
    assert _stored(service, record['id'])['deletedAt'] is None


def test_every_operation_announces_itself(main_window):
    service = _service(main_window)
    record = service.create(name='木头')
    seen = []
    service.changed.connect(seen.append)

    service.trash([record['id']])
    service.restore([record['id']])
    service.purge([record['id']])

    assert [change['operation'] for change in seen] == ['trash', 'restore', 'purge']
    assert seen[-1]['ids'] == [record['id']]
