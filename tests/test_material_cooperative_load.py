"""Card preparation yields without publishing partial or stale query results."""
import copy
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6 import QtCore

from prism.material_service import MaterialLibraryService
from prism.workspace_service import ResourceTreeService
from prism.widgets.material_cards import MaterialCards
from prism.widgets.widget_construction import WidgetConstruction, finish_construction


def _service(count):
    scene = SimpleNamespace(project_id='project', material_records=[])
    service = MaterialLibraryService(scene, ResourceTreeService('project'))
    for index in range(count):
        service.create(name=f'row {index:04}')
    return service


def test_no_hit_scan_yields_at_the_same_bound_as_matching_scan(qapp):
    service = _service(150)
    assert list(service.iter_card_record_batches(query='not-present', chunk_size=32)) == [[], [], [], []]
    batches = list(service.iter_card_record_batches(chunk_size=32))
    assert [len(batch) for batch in batches] == [32, 32, 32, 32, 22]
    assert [record for batch in batches for record in batch] == service.list_card_records()
    batches[0][0]['name'] = 'detached'
    assert service.records[0]['name'] == 'row 0000'


def test_cards_do_not_publish_partial_or_abandoned_results(qtbot, monkeypatch):
    monkeypatch.setattr(MaterialCards, '_ensure_web', lambda self: None)
    cards = MaterialCards()
    qtbot.addWidget(cards)
    rows = _service(150).list_card_records()
    cards.set_records(rows[:1], rows[0]['id'])
    before = (list(cards.assets), list(cards._record_ids), cards.generation)
    valid = [True]
    steps = cards.set_records_steps(rows, rows[-1]['id'], valid=lambda: valid[0])
    next(steps)
    assert (cards.assets, cards._record_ids, cards.generation) == before
    valid[0] = False
    finish_construction(steps)
    assert (cards.assets, cards._record_ids, cards.generation) == before
    cards.set_records(rows, rows[-1]['id'])
    assert cards._record_ids == [row['id'] for row in rows]
    assert [asset['name'] for asset in cards.assets] == [row['name'] for row in rows]
    assert cards.selected_id == rows[-1]['id']


def _populate(page, count=500):
    seed = page.records.create(name='source')
    page.records.records.extend(dict(copy.deepcopy(seed), id=f'row-{i}', name=f'row {i}')
                                for i in range(count - 1))
    page._loaded_revision = None


def test_cooperative_entry_yields_and_only_announces_complete_content(main_window, qtbot, monkeypatch):
    page = main_window.material_library.ensure_created()
    _populate(page)
    qtbot.waitUntil(lambda: page.materials.ready, timeout=15000)
    monkeypatch.setattr(WidgetConstruction, 'SLICE_SECONDS', 0)
    ticks, ready_sizes = [], []
    timer = QtCore.QTimer(page)
    timer.setInterval(0)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    page.content_ready.connect(lambda: ready_sizes.append(len(page.materials.assets)))
    job = page.open_folder_cooperatively()
    assert not page.is_content_ready() and not job.done
    page._surface_ready()  # An early browser-ready event cannot end the wait.
    assert ready_sizes == []
    qtbot.waitUntil(lambda: job.done, timeout=15000)
    timer.stop()
    assert job.error is None
    assert len(ticks) >= 16
    assert ready_sizes == [500] and page.is_content_ready()
    assert len(page.materials.assets) == 500


def test_sync_refresh_supersedes_pending_entry_and_keeps_latest_name(main_window, qtbot, monkeypatch):
    page = main_window.material_library.ensure_created()
    _populate(page)
    monkeypatch.setattr(WidgetConstruction, 'SLICE_SECONDS', 0)
    job = page.open_folder_cooperatively()
    qtbot.waitUntil(lambda: job.slices >= 3, timeout=15000)
    page.records.update(page.records.records[0]['id'], name='latest', notes='')
    assert job.cancel_requested and job.done
    assert page.materials.assets[0]['name'] == 'latest'
    qtbot.wait(25)
    assert page.materials.assets[0]['name'] == 'latest'


def test_entry_failure_is_reported_and_does_not_wait_for_a_timeout(main_window, qtbot, monkeypatch):
    page = main_window.material_library.ensure_created()
    ready = []
    page.content_ready.connect(lambda: ready.append(True))
    def broken(*args, **kwargs):
        raise RuntimeError('injected summary failure')
    monkeypatch.setattr(page.records, 'iter_card_record_batches', broken)
    job = page.open_folder_cooperatively()
    qtbot.waitUntil(lambda: job.done, timeout=15000)
    assert isinstance(job.error, RuntimeError)
    assert ready and not page.is_content_ready()
    assert '加载失败' in page.empty_title.text()


def test_entry_refresh_and_first_show_do_not_reinstall_identical_details(main_window, qtbot):
    page = main_window.material_library.ensure_created()
    seed = page.records.create(name='selected material')
    page.selected_id = seed['id']
    with patch.object(page.details, 'show_record', wraps=page.details.show_record) as show:
        page._show_details()
        page.refresh()
        page.show()
        assert show.call_count == 1
        # Value comparison also catches a caller correcting the owned payload
        # without emitting a new service revision.
        page.records.records[0]['notes'] = 'corrected source metadata'
        page._show_details()
        assert show.call_count == 2
        assert page.details.notes.toPlainText() == 'corrected source metadata'


def test_preview_receipt_does_not_cancel_or_complete_a_pending_entry(main_window, qtbot, monkeypatch):
    page = main_window.material_library.ensure_created()
    _populate(page)
    qtbot.waitUntil(lambda: page.materials.ready, timeout=15000)
    monkeypatch.setattr(WidgetConstruction, 'SLICE_SECONDS', 0)
    job = page.open_folder_cooperatively()
    qtbot.waitUntil(lambda: job.slices >= 3, timeout=15000)
    signals = []
    page.content_ready.connect(lambda: signals.append(True))
    page.refresh({'operation': 'preview', 'id': page.records.records[0]['id']})
    assert not job.cancel_requested and not job.done
    assert not page.is_content_ready() and signals == []
    qtbot.waitUntil(lambda: job.done, timeout=15000)
    assert page.is_content_ready() and len(page.materials.assets) == 500
