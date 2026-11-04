"""D9/10 delivery evidence: isolated source fixtures, real offscreen browser."""
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6 import QtWidgets

from prism.catalog_browser import CatalogRows
from prism.presentation_evidence import (QueryEvidence, add_presentation_observer,
                                        remove_presentation_observer)
from prism.widgets.asset_browser import AssetModel
from tests.test_catalog_browser_partial import prepare
from tests.test_asset_browser import photo


@pytest.fixture
def evidence_events(monkeypatch):
    monkeypatch.setenv('PRISM_PRESENTATION_DIAGNOSTIC', '1')
    events = []
    def observer(event, **fields):
        events.append((event, fields))
    add_presentation_observer(observer)
    yield events
    remove_presentation_observer(observer)


def latest(events, event, *, query_id=None, stage=None):
    return next(fields for kind, fields in reversed(events)
                if kind == event and (query_id is None or fields['query_id'] == query_id)
                and (stage is None or fields.get('stage') == stage))


def wait_published(qtbot, browser, events, expected, previous=None, timeout=20000):
    qtbot.waitUntil(lambda: browser._catalog_pending is None
        and browser.model.rowCount() == expected
        and browser.model.presentation_evidence is not None
        and browser.model.presentation_evidence.active
        and browser.model.presentation_evidence.query_id != previous
        and any(kind == 'asset_model_published' and
                fields['query_id'] == browser.model.presentation_evidence.query_id
                for kind, fields in events), timeout=timeout)
    return browser.model.presentation_evidence


def simple_rows(count, evidence):
    scope = SimpleNamespace(count=lambda **kw: count, total=lambda: count,
        iter_query=lambda **kw: iter(dict(id=i) for i in range(count)))
    return CatalogRows(scope, page_size=17, evidence=evidence).prepare()


def test_sampling_index_iteration_step_and_duplicates_have_distinct_counts():
    ev = QueryEvidence(diagnostic=True)
    rows = simple_rows(101, ev)
    before = ev.snapshot('sequence_access')
    for _ in range(4):
        assert len(rows) == 101 and bool(rows)
        rows.stats()
        ev.fields()
        ev.snapshot('read')
    assert ev.snapshot('sequence_access') == before
    assert [r['id'] for r in rows[:26]] == list(range(26))
    assert rows[-1]['id'] == 100
    assert [r['id'] for r in rows[1:9:2]] == [1, 3, 5, 7]
    assert len(list(rows)) == 101
    stats = ev.snapshot('sequence_access')
    assert stats['logical_rows'] == 132
    assert stats['unique_rows'] == 101
    assert ev.snapshot('consumer_consumed')['logical_rows'] == 0
    assert rows.stats()['delivered'] == 132
    assert ev.snapshot('read')['unique_rows'] == 101
    rows.close()


def test_production_budget_inactive_and_failing_observers(monkeypatch):
    def fail(*args, **kw):
        raise RuntimeError('observer failure must not escape')
    monkeypatch.delenv('PRISM_PRESENTATION_DIAGNOSTIC', raising=False)
    ev = QueryEvidence(observer=fail)
    ev.observe('consumer_consumed', range(1000))
    assert ev.snapshot('consumer_consumed')['logical_rows'] == 1000
    assert ev.snapshot('consumer_consumed')['unique_rows'] is None
    assert ev._counter('consumer_consumed').ids is None
    ev.record('asset_model_install_progress')
    ev = QueryEvidence(diagnostic=True, id_budget=2)
    ev.observe('consumer_consumed', [1, 1, 2, 3, 4])
    stats = ev.snapshot('consumer_consumed')
    assert stats['logical_rows'] == 5 and stats['unique_rows'] == 2
    assert stats['id_budget_exhausted'] and not stats['unique_rows_complete']
    assert stats['unique_rows_basis'] == 'lower_bound'
    ev.finish('catalog_query_cancelled', cancelled=True)
    ev.observe('consumer_consumed', [5])
    ev.record('asset_model_published', model_rows=0, final=True)
    assert ev.snapshot('consumer_consumed') == stats


def test_trace_session_identity_and_final_progress_survive_sampling(monkeypatch):
    from prism import performance_trace
    records = []
    observed = []
    monkeypatch.setattr(performance_trace, '_active', SimpleNamespace(session='trace-session'))
    monkeypatch.setattr(performance_trace, 'record', lambda event, **kw: records.append((event, kw)))
    ev = QueryEvidence(observer=lambda event, **kw: observed.append((event, kw)))
    for _ in range(10):
        ev.record('asset_model_install_progress', final=False)
    ev.record('asset_model_install_progress', final=True, complete=True)
    assert len(observed) == 11 and len(records) == 2
    assert all(fields['session_id'] == 'trace-session' for _, fields in records + observed)
    assert records[-1][1]['final']
    assert ev.fields()['schema_version'] == 2
    assert ev.fields()['presentation_schema_version'] == 1


@pytest.mark.parametrize('signal', ['modelAboutToBeReset', 'modelReset'])
def test_model_reset_reentrancy_publishes_only_final_identity(qapp, signal):
    events = []
    observer = lambda event, **kw: events.append((event, kw))
    model = AssetModel()
    old = QueryEvidence(diagnostic=True, observer=observer)
    new = QueryEvidence(diagnostic=True, observer=observer)
    first = [dict(title='old')]
    second = [dict(title='new'), dict(title='second')]
    fired = False
    def reenter():
        nonlocal fired
        if not fired:
            fired = True
            model.replace(second, evidence=new)
    getattr(model, signal).connect(reenter)
    assert not model.replace(first, evidence=old)
    assert model.items is second and model.presentation_evidence is new
    published = [f for e, f in events if e == 'asset_model_published']
    assert len(published) == 1
    assert published[0]['query_id'] == new.query_id and published[0]['model_rows'] == 2
    assert old.cancelled and not old.active


def test_two_views_share_model_identity_not_consumer_counts(qapp):
    model = AssetModel()
    ev = QueryEvidence(diagnostic=True)
    first, second = QtWidgets.QListView(), QtWidgets.QListView()
    first.setModel(model)
    second.setModel(model)
    model.replace([dict(title='one')], evidence=ev)
    assert first.model().presentation_evidence is second.model().presentation_evidence is ev
    assert ev.snapshot('consumer_consumed')['logical_rows'] == 0
    assert ev.snapshot('model')['unique_rows'] == 1
    first.deleteLater()
    second.deleteLater()


def test_real_browser_prefetched_sequence_is_not_consumer_and_old_timer_is_inert(
        main_window, tmp_path, qtbot, evidence_events):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(i, 'pixmap', dict(canvasId='A', title=f'item{i}')) for i in range(600)])
    callbacks = []
    # Force one actual loop advance per 4ms slice, but still prefetch 256.
    import time
    clock = time.perf_counter
    ticks = iter(range(10000))
    with patch('prism.widgets.asset_browser.QtCore.QTimer.singleShot',
               side_effect=lambda ms, cb: callbacks.append(cb)), \
         patch('time.perf_counter', side_effect=lambda: clock() + next(ticks) * .005):
        browser.refresh()
        qtbot.waitUntil(lambda: bool(callbacks), timeout=15000)
    pending = browser._catalog_pending
    ev = pending['evidence']
    assert ev.snapshot('sequence_access')['logical_rows'] == 256
    assert ev.snapshot('consumer_consumed')['logical_rows'] == pending['offset'] == 1
    assert ev.snapshot('consumer_consumed')['unique_rows'] == 1
    browser._cancel_catalog_query()
    frozen = ev.snapshot('consumer_consumed')
    for callback in callbacks:
        callback()
    assert ev.snapshot('consumer_consumed') == frozen
    assert not ev.active and ev.cancelled
    browser.mode = 'untagged'
    browser.refresh()
    new = wait_published(qtbot, browser, evidence_events, 600, previous=ev.query_id)
    before = new.snapshot('consumer_consumed')
    browser._catalog_rows_ready((pending['token'], pending['rows'], None))
    browser._install_catalog_rows(pending)
    assert new.snapshot('consumer_consumed') == before
    assert new.read_id == ev.read_id and new.read_reused
    browser._catalog_rows_ready(((browser._catalog_generation, scene.project_id), pending['rows'], None))
    assert new.snapshot('consumer_consumed') == before


def test_real_browser_overlay_filters_cache_replay_and_search_domain(
        main_window, tmp_path, qtbot, evidence_events):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(1, 'pixmap', dict(canvasId='A', title='needle old', tags=[])),
         (2, 'pixmap', dict(canvasId='B', title='needle tagged', tags=['tag'])),
         (3, 'pixmap', dict(canvasId='A', title='needle three', tags=[]))])
    edited = photo(main_window, 'A', 'needle edited')
    edited.save_id = 1
    edited._tags = ['edited']
    added = photo(main_window, 'B', 'needle new')
    added.save_id = None
    browser.open('all')
    first = wait_published(qtbot, browser, evidence_events, 4)
    assert first.snapshot('consumer_consumed')['unique_rows'] == 3
    published = latest(evidence_events, 'asset_model_published', query_id=first.query_id)
    assert published['model_rows'] == 4 and published['overlay_rows'] == 1
    assert published['new_rows'] == 1 and published['model_unique_ids'] == 4
    event_count = len(evidence_events)
    browser.open('all')
    assert browser.model.presentation_evidence is first
    assert len(evidence_events) == event_count  # No fabricated reread.
    browser.open('untagged')
    second = wait_published(qtbot, browser, evidence_events, 2, previous=first.query_id)
    assert not first.active and first.cancelled
    assert second.source_query_id == first.query_id and second.read_id == first.read_id
    assert second.read_reused and second.read_fields()['read_logical_rows_this_query'] == 0
    assert second.snapshot('consumer_consumed')['unique_rows'] == 3
    assert second.snapshot('sequence_access')['unique_rows'] == 3
    assert second.source_revision == first.source_revision
    browser.mode = 'all'
    browser.search.setText('needle')
    browser.timer.stop()
    with patch.object(browser.view.category_panel, 'project_search_results',
                      return_value=[dict(kind='document', item_id='doc', title='needle doc')]):
        browser.refresh()
        third = wait_published(qtbot, browser, evidence_events, 5, previous=second.query_id)
    assert third.snapshot('consumer_consumed')['unique_rows'] == 3
    published = latest(evidence_events, 'asset_model_published', query_id=third.query_id)
    assert published['model_rows'] == 5 and published['search_hit_rows'] == 1
    assert str(tmp_path) not in json.dumps(third.fields())
    scene.metadata_changed.emit()
    browser.timer.stop()
    browser.refresh()
    fourth = wait_published(qtbot, browser, evidence_events, 4, previous=third.query_id)
    assert fourth.source_revision != third.source_revision


def test_real_browser_empty_and_failure_are_explicit(main_window, tmp_path, qtbot, evidence_events):
    scope, scene, browser = prepare(main_window, tmp_path, [])
    browser.refresh()
    first = wait_published(qtbot, browser, evidence_events, 0)
    read = latest(evidence_events, 'catalog_rows_read_evidence', query_id=first.query_id)
    consumed = latest(evidence_events, 'catalog_rows_delivered', query_id=first.query_id,
                      stage='consumer_consumed')
    assert read['logical_rows'] == read['unique_rows'] == 0 and read['complete']
    assert consumed['logical_rows'] == consumed['unique_rows'] == 0 and consumed['final']
    browser._catalog_scalar_cache = None
    browser._catalog_result_key = None
    with patch.object(scope, 'count', side_effect=RuntimeError('private path not logged')):
        browser.refresh()
        qtbot.waitUntil(lambda: any(e == 'catalog_query_failed' for e, _ in evidence_events), timeout=10000)
    failure = latest(evidence_events, 'catalog_query_failed')
    assert failure['final'] and not failure['complete']
    assert not browser._catalog_evidence.active
    assert browser.model.presentation_evidence is None
    assert 'private path' not in json.dumps(failure)


def test_late_read_keeps_original_query_identity():
    events = []
    ev = QueryEvidence(diagnostic=True, observer=lambda e, **f: events.append((e, f)))
    rows = simple_rows(20, ev)
    started, release = threading.Event(), threading.Event()
    def blocked_generator():
        started.set()
        assert release.wait(5)
        yield dict(id=999)
    with rows._lock:
        rows._generator = blocked_generator()
    thread = threading.Thread(target=rows._pull)
    thread.start()
    assert started.wait(5)
    ev.finish('catalog_query_cancelled', cancelled=True)
    new = QueryEvidence(diagnostic=True, observer=lambda e, **f: events.append((e, f)))
    rows.bind_evidence(new)
    before = len(events)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert not any(e == 'catalog_rows_read_evidence' and f['query_id'] == new.query_id
                   for e, f in events[before:])
    assert new.snapshot('consumer_consumed')['logical_rows'] == 0
    rows.close()


def test_real_browser_100000_row_delivery_and_model_install(
        main_window, tmp_path, qtbot, evidence_events):
    count = 100000
    scope, scene, browser = prepare(main_window, tmp_path,
        ((i, 'pixmap', dict(canvasId='A' if i < count//2 else 'B',
                            title=f'Asset {i:06d}', imageWidth=16, imageHeight=8, tags=[]))
         for i in range(count)))
    source_hash = hashlib.sha256(scope.project_path.read_bytes()).hexdigest()
    # Genuine browser, catalog paging, scalar filtering, ImageRecord proxies,
    # sliced GUI consumption and real AssetModel.reset. No mocked installer.
    with patch.object(scope.index, 'details', side_effect=AssertionError('full detail read')), \
         patch('prism.source_store.read_archive', side_effect=AssertionError('original BLOB read')):
        browser.refresh()
        ev = wait_published(qtbot, browser, evidence_events, count, timeout=240000)
    read = latest(evidence_events, 'catalog_rows_read_evidence', query_id=ev.query_id)
    delivery = latest(evidence_events, 'catalog_rows_delivered', query_id=ev.query_id,
                      stage='consumer_consumed')
    published = latest(evidence_events, 'asset_model_published', query_id=ev.query_id)
    assert read['unique_rows'] == delivery['unique_rows'] == published['model_rows'] == count
    assert delivery['logical_rows'] == count
    assert published['model_unique_ids'] == count
    assert read['complete'] and delivery['complete'] and published['complete']
    assert read['unique_rows_complete'] and delivery['unique_rows_complete']
    assert all(fields['query_id'] == ev.query_id and fields['source_revision'] == ev.source_revision
               and fields['session_id'] == ev.session_id for fields in (read, delivery, published))
    assert len(list(scene.iter_assets())) == 0
    assert hashlib.sha256(scope.project_path.read_bytes()).hexdigest() == source_hash
    assert all(getattr(type(item), '_catalog_proxy', False) for item in browser.model.items)
    assert scope.source_state() is not None
    print('D100000 ' + json.dumps(dict(query_id=ev.query_id,
        source_revision=ev.source_revision, read_unique=read['unique_rows'],
        read_logical=read['logical_rows'], consumer_unique=delivery['unique_rows'],
        consumer_logical=delivery['logical_rows'], model_rows=published['model_rows'],
        model_unique=published['model_unique_ids'],
        sequence_logical=ev.snapshot('sequence_access')['logical_rows'])))


@pytest.mark.parametrize('signal', ['modelAboutToBeReset', 'modelReset'])
def test_real_browser_reentrant_publication_finishes_new_query(
        main_window, tmp_path, qtbot, evidence_events, signal):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', tags=[])),
        (2, 'pixmap', dict(canvasId='B', tags=['tag']))])
    # Prime a complete scalar cache, then switch scope synchronously during
    # the first reset of a real install. The nested model.replace is queued.
    browser.refresh()
    first = wait_published(qtbot, browser, evidence_events, 2)
    fired = False
    def reenter():
        nonlocal fired
        if not fired:
            fired = True
            browser.mode = 'all'
            browser.refresh()
    getattr(browser.model, signal).connect(reenter)
    browser.mode = 'untagged'
    browser.refresh()
    last = wait_published(qtbot, browser, evidence_events, 2, previous=first.query_id)
    assert fired and browser._catalog_pending is None
    assert browser._catalog_result_items is browser.model.items
    assert browser._catalog_result_key == browser._catalog_requested_key
    assert last.snapshot('consumer_consumed')['logical_rows'] == 2
    assert sum(e == 'asset_model_published' and f['query_id'] == last.query_id
               for e, f in evidence_events) == 1


def test_real_browser_failing_evidence_observer_preserves_business(
        main_window, tmp_path, qtbot, evidence_events):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (i, 'pixmap', dict(canvasId='A')) for i in range(9)])
    def fail(event, **fields):
        raise RuntimeError('broken evidence sink')
    add_presentation_observer(fail)
    try:
        browser.refresh()
        ev = wait_published(qtbot, browser, evidence_events, 9)
    finally:
        remove_presentation_observer(fail)
    assert ev.snapshot('consumer_consumed')['unique_rows'] == 9
    assert browser.model.rowCount() == 9 and not list(scene.iter_assets())


def test_observer_reentry_cannot_trace_post_cancel_result_or_mutate_scope(monkeypatch):
    from prism import performance_trace
    events = []
    monkeypatch.setattr(performance_trace, 'record', lambda e, **f: events.append((e, f)))
    ev = QueryEvidence(dict(mode='all'), diagnostic=True)
    def cancel(event, **fields):
        fields['scope']['mode'] = 'mutated'
        if event == 'asset_model_install_progress':
            ev.finish('catalog_query_cancelled', cancelled=True)
        if event == 'catalog_query_cancelled':
            ev.observe('consumer_consumed', [99])
            ev.record('asset_model_published', final=True)
            ev.finish('catalog_query_cancelled', cancelled=True)
    ev.observer = cancel
    ev.record('asset_model_install_progress')
    assert [e for e, f in events] == ['catalog_query_cancelled']
    assert ev.fields()['scope'] == dict(mode='all')
    assert ev.snapshot('consumer_consumed')['logical_rows'] == 0


def test_cancelled_cached_read_restores_full_prepared_count():
    from prism.project_open_scope import OpenScopeCancelled
    stop = threading.Event()
    def query(**kwargs):
        for i in range(101):
            if kwargs['cancel']():
                raise OpenScopeCancelled()
            yield dict(id=i)
    scope = SimpleNamespace(count=lambda **kw: 101, total=lambda: 101, iter_query=query)
    old = QueryEvidence(diagnostic=True)
    rows = CatalogRows(scope, page_size=17, cancel=stop.is_set, evidence=old).prepare()
    stop.set()
    with pytest.raises(IndexError):
        rows[17]
    assert rows.stats()['cancelled'] and len(rows) == 17
    old.finish('catalog_query_cancelled', cancelled=True)
    new = QueryEvidence(diagnostic=True)
    rows.bind_evidence(new, lambda: False)
    assert len(rows) == 101
    assert [row['id'] for row in rows] == list(range(101))
    assert new.snapshot('sequence_access')['unique_rows'] == 101
    assert new.read_id == old.read_id and new.snapshot('read')['unique_rows'] == 101
    rows.close()


def test_late_cancelled_producer_pass_and_blocked_access_do_not_poison_new_query():
    from prism.project_open_scope import OpenScopeCancelled
    stop, started, release = threading.Event(), threading.Event(), threading.Event()
    passes = 0
    def query(**kwargs):
        nonlocal passes
        passes += 1
        old_pass = passes == 1
        for i in range(101):
            if old_pass and i == 17:
                started.set()
                assert release.wait(5)
                raise OpenScopeCancelled()
            yield dict(id=i)
    scope = SimpleNamespace(count=lambda **kw: 101, total=lambda: 101, iter_query=query)
    old = QueryEvidence(diagnostic=True)
    rows = CatalogRows(scope, page_size=17, cancel=stop.is_set, evidence=old).prepare()
    result = []
    consumer = threading.Thread(target=lambda: result.append(rows[17]))
    consumer.start()
    assert started.wait(5)
    stop.set()
    old.finish('catalog_query_cancelled', cancelled=True)
    new = QueryEvidence(diagnostic=True)
    rows.bind_evidence(new, lambda: False)
    release.set()
    consumer.join(5)
    assert not consumer.is_alive() and result[0]['id'] == 17
    assert not rows.stats()['cancelled'] and len(rows) == 101
    assert new.snapshot('sequence_access')['logical_rows'] == 0
    assert len(list(rows)) == 101
    assert new.snapshot('sequence_access')['unique_rows'] == 101
    rows.close()
