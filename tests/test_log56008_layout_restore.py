"""Content-keyed hot layout restoration through the real browser/model."""
from dataclasses import replace
from unittest.mock import Mock
from PyQt6 import QtCore
from tests.test_catalog_browser_partial import prepare, wait_complete


def loaded(main_window, tmp_path, qtbot, count=12):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (i, 'pixmap', dict(canvasId='A' if i%2 else 'B', imageWidth=100,
                          imageHeight=50, title=str(i), tags=['T'] if i%2 else []))
        for i in range(count)])
    browser.open('all')
    wait_complete(qtbot, browser, count)
    return browser


def test_real_a_b_a_restores_geometry_without_arranging(main_window, tmp_path, qtbot, monkeypatch):
    browser = loaded(main_window, tmp_path, qtbot)
    host = browser.list
    before = [host._card_rect(i) for i in range(12)]
    first_items = browser.model.items
    browser.open('untagged')
    wait_complete(qtbot, browser, 6)
    arrange = Mock(wraps=host.arrange)
    monkeypatch.setattr(host, 'arrange', arrange)
    browser.open('all')
    wait_complete(qtbot, browser, 12)
    assert browser.model.items is not first_items
    assert [host._card_rect(i) for i in range(12)] == before
    arrange.assert_not_called()
    assert host._item_rows._items is browser.model.items


def test_snapshot_holds_no_model_or_facade_generation(main_window, tmp_path, qtbot):
    browser = loaded(main_window, tmp_path, qtbot)
    state = browser.list.snapshot_catalog_layout(browser.model.items,
                                                  token=browser.model.layout_token)
    assert state is not None
    assert 'items' not in state and 'cards' not in state
    assert type(state['item_rows']) is dict  # no _RowIndex._items reference


def test_old_layout_rejects_geometry_and_row_order_changes(main_window, tmp_path, qtbot):
    browser = loaded(main_window, tmp_path, qtbot)
    host = browser.list
    key = browser._catalog_results.get(browser._catalog_result_digest)['query_key']
    token = browser.model.layout_token
    snapshot = host.snapshot_catalog_layout(browser.model.items, token=token)
    reversed_token = browser._catalog_layout_token_for(list(reversed(browser.model.items)), key)
    assert reversed_token != token
    browser.model.items[0].crop = QtCore.QRectF(0, 0, 40, 100)
    assert browser._catalog_layout_token_for(browser.model.items, key) != token
    host.cards[0].setPos(600, 700)
    assert not host.restore_catalog_layout(snapshot)


def test_stale_project_open_or_viewport_snapshot_rejected(main_window, tmp_path, qtbot):
    browser = loaded(main_window, tmp_path, qtbot)
    host = browser.list
    token = browser.model.layout_token
    state = host.snapshot_catalog_layout(browser.model.items, token=token)
    assert not host.restore_catalog_layout(dict(state, width=state['width']+1))
    assert not host.restore_catalog_layout(dict(state, dpr=state['dpr']+1))
    assert not host.restore_catalog_layout(dict(state, token=replace(token, open_epoch='old')))


def test_republish_same_identity_does_not_emit_all_rows_metadata_changed(main_window, tmp_path, qtbot):
    browser = loaded(main_window, tmp_path, qtbot)
    changed = []
    browser.model.dataChanged.connect(lambda *args: changed.append(args))
    items = list(browser.model.items)
    browser.model.replace(items, layout_token=browser.model.layout_token)
    assert changed == []
    browser.model.replace(list(items))  # legacy callers still request refresh
    assert len(changed) == 1


def test_large_identity_capture_yields_and_rejects_late_old_generation(main_window, tmp_path, qtbot, monkeypatch):
    browser = loaded(main_window, tmp_path, qtbot)
    key = browser._catalog_results.get(browser._catalog_result_digest)['query_key']
    pending = {'phase': 'test'}
    monkeypatch.setattr(browser, '_catalog_install_valid', lambda candidate: candidate is pending)
    done, queued = [], []
    monkeypatch.setattr(QtCore.QTimer, 'singleShot', lambda delay, callback: queued.append(callback))
    browser._prepare_catalog_layout_token(pending, browser.model.items*100, key, done.append)
    assert done == [] and len(queued) == 1
    queued.pop(0)()
    assert done == [] and len(queued) == 1  # at most 256 rows per callback
    monkeypatch.setattr(browser, '_catalog_install_valid', lambda candidate: False)
    queued.pop(0)()
    assert done == []


def test_other_query_manual_move_does_not_discard_saved_manual_layout(main_window, tmp_path, qtbot):
    browser = loaded(main_window, tmp_path, qtbot)
    host = browser.list
    host.cards[0].setPos(700, -200)
    before = host._card_rect(0)
    browser.open('untagged')
    wait_complete(qtbot, browser, 6)
    host.cards[0].setPos(-500, 300)
    browser.open('all')
    wait_complete(qtbot, browser, 12)
    assert host._card_rect(0) == before


def test_equal_hot_reference_array_does_not_strand_metadata_refresh(main_window, tmp_path, qtbot):
    browser = loaded(main_window, tmp_path, qtbot, count=300)
    host = browser.list
    host._model_data_changed(browser.model.index(0), browser.model.index(299))
    state = host._metadata_refresh_state
    assert state is not None
    browser.model.replace(list(browser.model.items), layout_token=browser.model.layout_token)
    assert browser.model.items is not state['items']
    qtbot.waitUntil(lambda: host._metadata_refresh_state is None, timeout=5000)
    assert state['updated'] == 300
    assert state['items'] is browser.model.items


def test_geometry_delivery_between_hash_slices_cannot_certify_old_layout(main_window, tmp_path, qtbot, monkeypatch):
    browser = loaded(main_window, tmp_path, qtbot)
    key = browser._catalog_results.get(browser._catalog_result_digest)['query_key']
    pending, queued, done = {}, [], []
    monkeypatch.setattr(browser, '_catalog_install_valid', lambda candidate: candidate is pending)
    monkeypatch.setattr(QtCore.QTimer, 'singleShot', lambda delay, cb: queued.append(cb))
    browser._prepare_catalog_layout_token(pending, browser.model.items*100, key, done.append)
    queued.pop(0)()
    browser.list._geometry_version += 1
    queued.pop(0)()
    assert done == [None]
