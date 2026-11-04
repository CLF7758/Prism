"""D-group closeout: pass cancellation, reset supersession and publish counts."""
import threading
from types import SimpleNamespace

import pytest

from prism.catalog_browser import CatalogRows
from prism.presentation_evidence import QueryEvidence
from prism.project_open_scope import OpenScopeCancelled
from prism.widgets.asset_browser import AssetModel


def observed_evidence(events, **kwargs):
    return QueryEvidence(observer=lambda event, **fields: events.append((event, fields)),
                         diagnostic=kwargs.pop('diagnostic', True), **kwargs)


def publications(events):
    return [fields for event, fields in events if event == 'asset_model_published']


def test_generator_retains_its_cancel_callable_after_rebind():
    old_cancel, new_cancel = threading.Event(), threading.Event()
    seen = []

    def query(*, cancel, **kwargs):
        for index in range(3):
            seen.append(cancel())
            if seen[-1]:
                raise OpenScopeCancelled()
            yield dict(id=index)

    scope = SimpleNamespace(iter_query=query)
    old = QueryEvidence(diagnostic=True)
    rows = CatalogRows(scope, cancel=old_cancel.is_set, evidence=old)
    generator = rows._new_generator()
    assert next(generator)['id'] == 0
    old_cancel.set()
    rows.bind_evidence(QueryEvidence(diagnostic=True), new_cancel.is_set)
    try:
        with pytest.raises(OpenScopeCancelled):
            next(generator)
        assert seen == [False, True]
        assert not new_cancel.is_set()
    finally:
        generator.close()
        rows.close()


@pytest.mark.parametrize('cooperative', [True, False])
def test_normal_producer_rebind_drops_old_pass_without_rebasing_reads(cooperative):
    old_cancel, entered, release = (threading.Event() for _ in range(3))
    events, seen, result, failures = [], [], [], []
    passes = 0

    def query(*, cancel, **kwargs):
        nonlocal passes
        passes += 1
        old_pass = passes == 1
        for index in range(6):
            if old_pass and index == 2:
                entered.set()
                assert release.wait(5)
                seen.append(cancel())
                if cooperative and seen[-1]:
                    raise OpenScopeCancelled()
            # A non-cooperative old read may do real work, never deliver it.
            yield dict(id=999 + index if old_pass and index >= 2 else index)

    scope = SimpleNamespace(count=lambda **kw: 6, iter_query=query)
    old = observed_evidence(events)
    rows = CatalogRows(scope, page_size=2, cancel=old_cancel.is_set,
                       evidence=old).prepare()

    def consume():
        try:
            result.append(rows[2])
        except Exception as error:
            failures.append(error)

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    try:
        assert entered.wait(5)
        old_cancel.set()
        old.finish('catalog_query_cancelled', cancelled=True)
        new = observed_evidence(events)
        rows.bind_evidence(new, lambda: False)
        after_bind = len(events)
        release.set()
        consumer.join(5)
        assert not consumer.is_alive() and not failures
        assert seen == [True]
        assert result == [dict(id=2)]
        assert passes == 2 and not rows.stats()['cancelled']
        assert new.snapshot('sequence_access')['logical_rows'] == 0
        assert new.snapshot('consumer_consumed')['logical_rows'] == 0
        assert [row['id'] for row in rows] == list(range(6))
        assert new.snapshot('sequence_access')['unique_rows'] == 6
        assert new.read_id == old.read_id and new.source_query_id == old.query_id
        expected_reads = 8 if cooperative else 10
        assert new.read_fields()['rows_read'] == expected_reads
        assert new.read_fields()['read_logical_rows_this_query'] == 6
        assert all(fields['read_logical_rows_this_query'] <= 6
                   for event, fields in events[after_bind:]
                   if event == 'catalog_rows_read_evidence' and fields['query_id'] == new.query_id)
    finally:
        release.set()
        consumer.join(5)
        rows.close()
        if rows._producer is not None:
            rows._producer.join(5)


@pytest.mark.parametrize('signal', ['modelAboutToBeReset', 'modelReset'])
def test_reset_callback_cancels_overwritten_pending_evidence(qapp, signal):
    events = []
    model = AssetModel()
    first, discarded, last = (observed_evidence(events) for _ in range(3))
    final_rows = [dict(title='C')]
    fired = False

    def queue_two():
        nonlocal fired
        if not fired:
            fired = True
            assert not model.replace([dict(title='B')], evidence=discarded)
            assert not model.replace(final_rows, evidence=last)

    getattr(model, signal).connect(queue_two)
    assert not model.replace([dict(title='A')], evidence=first)
    assert fired and model.items is final_rows and model.presentation_evidence is last
    assert not first.active and first.cancelled
    assert not discarded.active and discarded.cancelled
    assert last.active and not last.cancelled
    assert [fields['query_id'] for fields in publications(events)] == [last.query_id]
    before = len(events)
    discarded.record('asset_paint_frame', complete=True)
    assert len(events) == before  # No ghost paint under the dropped identity.


@pytest.mark.parametrize('signal', ['modelAboutToBeReset', 'modelReset'])
def test_pending_cancel_observer_reentry_keeps_last_request(qapp, signal):
    events = []
    model = AssetModel()
    first, discarded, overwritten, last = (observed_evidence(events) for _ in range(4))
    final_rows = [dict(title='D')]
    fired = False

    def cancel_observer(event, **fields):
        events.append((event, fields))
        if event == 'catalog_query_cancelled':
            model.replace(final_rows, evidence=last)

    discarded.observer = cancel_observer

    def queue_two():
        nonlocal fired
        if not fired:
            fired = True
            model.replace([dict(title='B')], evidence=discarded)
            model.replace([dict(title='C')], evidence=overwritten)

    getattr(model, signal).connect(queue_two)
    assert not model.replace([dict(title='A')], evidence=first)
    assert model.items is final_rows and model.presentation_evidence is last
    assert all(ev.cancelled and not ev.active for ev in (first, discarded, overwritten))
    assert last.active and not last.cancelled
    assert [fields['query_id'] for fields in publications(events)] == [last.query_id]


@pytest.mark.parametrize('signal', ['modelAboutToBeReset', 'modelReset'])
def test_pending_overwrite_does_not_cancel_shared_evidence(qapp, signal):
    events = []
    model = AssetModel()
    evidence = observed_evidence(events)
    final_rows = [dict(title='C')]
    fired = False

    def queue_two():
        nonlocal fired
        if not fired:
            fired = True
            model.replace([dict(title='B')], evidence=evidence)
            model.replace(final_rows, evidence=evidence)

    getattr(model, signal).connect(queue_two)
    assert not model.replace([dict(title='A')], evidence=evidence)
    assert model.items is final_rows and model.presentation_evidence is evidence
    assert evidence.active and not evidence.cancelled
    assert len(publications(events)) == 1


def test_same_evidence_second_publication_is_non_cumulative(qapp):
    events = []
    model = AssetModel()
    evidence = observed_evidence(events)
    for title in ('first', 'second'):
        assert model.replace([dict(title=title)], evidence=evidence)
        assert evidence.snapshot('model')['unique_rows'] == 1
    published = publications(events)
    assert len(published) == 2
    assert all(fields['model_rows'] == fields['model_unique_ids'] == 1 for fields in published)
    assert all(not fields['model_unique_ids_cumulative'] and
               fields['model_unique_ids_complete'] for fields in published)


def test_publication_budget_and_duplicates_are_local_to_each_model(qapp):
    events = []
    model = AssetModel()
    evidence = observed_evidence(events, id_budget=2)
    a, b, c, d = (SimpleNamespace(save_id=key) for key in 'abcd')
    assert model.replace([a, a, b, c], evidence=evidence)
    first = publications(events)[-1]
    assert first['model_rows'] == 4 and first['model_unique_ids'] == 2
    assert first['id_budget_exhausted'] and not first['model_unique_ids_complete']
    assert model.replace([d, d], evidence=evidence)
    second = publications(events)[-1]
    assert second['model_rows'] == 2 and second['model_unique_ids'] == 1
    assert not second['id_budget_exhausted'] and second['model_unique_ids_complete']
    assert evidence.snapshot('model')['logical_rows'] == 2
    assert first['id_budget_exhausted']  # The earlier published snapshot is unchanged.


def test_production_publication_does_not_collect_model_ids(qapp, monkeypatch):
    events = []
    model = AssetModel()
    evidence = observed_evidence(events, diagnostic=False)

    def unexpected_id_probe(item):
        raise AssertionError('Production must not collect model IDs')

    monkeypatch.setattr('prism.widgets.asset_browser.model_item_id', unexpected_id_probe)
    for title in ('first', 'second'):
        assert model.replace([dict(title=title)], evidence=evidence)
        assert evidence._counter('model').ids is None
    assert len(publications(events)) == 2
    assert all(fields['model_unique_ids'] is None and not fields['model_unique_ids_complete']
               for fields in publications(events))
