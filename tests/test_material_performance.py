import json
import time
from unittest.mock import patch

from PIL import Image
from PyQt6 import QtCore
from prism.material_cache import MaterialCache, MaterialThumbnailCache
from prism.widgets.material_cards import material_thumbnail
from prism.widgets.material_task import MaterialTask
from prism import performance_trace


def test_late_task_signals_cannot_touch_destroyed_library(main_window, monkeypatch):
    from PyQt6 import sip
    from unittest.mock import Mock
    page = main_window.material_library
    jobs = []
    monkeypatch.setattr(page._worker_pool, 'start', jobs.append)
    record = page.records.create()
    callback, finished = Mock(), Mock()
    page._task(lambda: 'late result', callback, record_id=record['id'], finally_callback=finished)
    job = jobs[-1]
    sip.delete(page)
    job.signals.result.emit('late result')
    job.signals.error.emit('late failure')
    job.signals.finished.emit()
    callback.assert_not_called()
    finished.assert_not_called()
    assert job not in page._jobs


def test_current_preview_overtakes_queued_card_thumbnails(main_window, qtbot):
    import threading
    page = main_window.material_library.ensure_created()
    record = page.records.create()
    pool = QtCore.QThreadPool(page)
    pool.setMaxThreadCount(1)
    page._worker_pool = pool
    entered, release = threading.Event(), threading.Event()
    started = []
    def hold():
        entered.set()
        assert release.wait(10)
    try:
        page._task(hold, lambda value: None, record_id=record['id'])
        qtbot.waitUntil(entered.is_set)
        for kind in ('card_thumbnail', 'card_thumbnail', 'slot_icon', 'render_inputs'):
            page._task(lambda name=kind: started.append(name), lambda value: None,
                       record_id=record['id'], kind=kind)
        release.set()
        qtbot.waitUntil(lambda: not page._jobs, timeout=10000)
        assert started == ['render_inputs', 'slot_icon', 'card_thumbnail', 'card_thumbnail']
    finally:
        release.set()
        pool.waitForDone(2000)


def test_card_decodes_leave_an_existing_worker_for_interactive_preview(main_window, qtbot):
    import threading
    page = main_window.material_library.ensure_created()
    record = page.records.create()
    entered, release, rendered = threading.Event(), threading.Event(), threading.Event()
    def card():
        entered.set()
        assert release.wait(5)
    try:
        for _ in range(3):
            page._task(card, lambda value: None, record_id=record['id'], kind='card_thumbnail')
        qtbot.waitUntil(entered.is_set)
        page._task(lambda: rendered.set(), lambda value: None,
                   record_id=record['id'], kind='render_inputs')
        qtbot.waitUntil(rendered.is_set, timeout=2000)
        assert not release.is_set(), 'Preview must start before the card read finishes'
    finally:
        release.set()
        qtbot.waitUntil(lambda: not page._jobs, timeout=5000)


def test_thumbnail_replacement_same_attachment_id_invalidates(tmp_path):
    a = tmp_path/'a.png'
    b = tmp_path/'b.png'
    Image.new('RGB', (256, 256), 'red').save(a)
    Image.new('RGB', (256, 256), 'green').save(b)
    record = {'preview': {'thumbnailAttachmentId': 1}}
    attachments = {1: ('preview.png', 'image/png', a.read_bytes())}
    cache = MaterialThumbnailCache()
    with patch('prism.widgets.material_cards.QtGui.QImage.fromData', wraps=__import__('PyQt6.QtGui', fromlist=['QImage']).QImage.fromData) as decode:
        first = cache.get(record, attachments, material_thumbnail)
        for _ in range(30):
            assert cache.get(record, attachments, material_thumbnail) == first
        assert decode.call_count == 1
        attachments[1] = ('preview.png', 'image/png', b.read_bytes())
        assert cache.get(record, attachments, material_thumbnail) != first
        assert decode.call_count == 2


def test_cache_budget_and_recently_used_eviction():
    cache = MaterialCache(2, 8)
    cache['a'] = b'1234'
    cache['b'] = b'5678'
    assert cache['a'] == b'1234'
    cache['c'] = b'0000'
    assert cache.get('b') is None
    assert cache.bytes == 8 and len(cache) == 2
    cache['too-large'] = b'x'*9
    assert len(cache) == 2 and cache.bytes == 8
    cache.clear()
    assert cache.bytes == 0


def test_obsolete_task_does_not_decode_or_emit_result(qapp):
    results = []
    with patch('prism.widgets.material_task.record') as record:
        task = MaterialTask(lambda: results.append('decoded'), obsolete=lambda: True)
        task.signals.result.connect(results.append)
        task.run()
        assert not results
        assert any(call.args[0] == 'material_task_cancelled' for call in record.call_args_list)


def test_material_logs_correlate_browser_tasks_and_cache(main_window, qapp, qtbot, tmp_path):
    trace = performance_trace.install(qapp, main_window, tmp_path)
    try:
        main_window.show()
        main_window.open_material_library()
        page = main_window.material_library
        materials = [page.records.create(name=f'item {i}') for i in range(10)]
        with patch.object(page.materials, 'set_records', wraps=page.materials.set_records) as rebuild:
            for material in materials:
                page._selection_changed(material['id'])
                page._auto_timer.stop()
            assert rebuild.call_count == 0
        page.materials.bridge.metrics(json.dumps({'long_tasks': 1, 'longest_task_ms': 91}))
        page._task(lambda: 42, lambda value: None, kind='test')
        qtbot.waitUntil(lambda: not page._jobs, timeout=15000)
        trace.summary_time -= 2
        # Normal mode samples cache/process state every five seconds; advancing
        # only the one-second summary clock cannot request that sample.
        trace._last_periodic_sample -= 6
        trace._heartbeat()
    finally:
        trace.close()
    entries = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    assert any(e['event'] == 'material_browser' and e['metrics']['long_tasks'] == 1 for e in entries)
    queued = next(e for e in entries if e['event'] == 'material_task_queued' and e['task_kind'] == 'test')
    assert any(e['event'] == 'material_task_start' and e['task'] == queued['task'] for e in entries)
    assert any(e['event'] == 'material_task_work' and e['task'] == queued['task'] for e in entries)
    assert any(e['event'] == 'material_cache' for e in entries)


def test_preview_completion_does_not_rebuild_catalogue(main_window, qtbot, tmp_path):
    from prism.material_textures import render_signature
    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library
    material = page.records.create()
    image = tmp_path/'preview.png'
    Image.new('RGB', (256, 256), 'blue').save(image)
    with patch.object(page.materials, 'set_records', wraps=page.materials.set_records) as rebuild:
        assert page.records.save_preview(material['id'], render_signature(material), image.read_bytes())
        assert rebuild.call_count == 0
    assert page.materials.assets[0]['thumbnail'].startswith('data:image/png')


def test_lazy_originals_are_read_only_by_workers(main_window, qtbot, tmp_path):
    import threading
    from prism.fileio.attachments_store import StoredAttachments
    from prism.material_textures import prepare_texture
    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library
    material = page.records.create()
    source = tmp_path/'color.png'
    Image.new('RGB',(64,64),'green').save(source)
    page.records.configure(material['id'], slot='basecolor', prepared=prepare_texture(source))
    record = page.records.get(material['id'])
    attachments = page.window_host.view.scene.attachments
    blob = attachments[record['files'][0]['attachmentId']][2]
    threads = []
    def load(key):
        threads.append(threading.get_ident())
        return blob
    lazy = StoredAttachments(loader=load)
    key = record['files'][0]['attachmentId']
    lazy._meta[key] = ('color.png','image/png')
    page.window_host.view.scene.attachments = lazy
    page._selection_changed(material['id'])
    page._auto_timer.stop()
    qtbot.waitUntil(lambda: not page._jobs, timeout=15000)
    assert threads and all(thread != threading.get_ident() for thread in threads)


def test_offscreen_card_metadata_never_loads_lazy_preview_blobs(main_window):
    from prism.fileio.attachments_store import StoredAttachments
    reads = []
    store = StoredAttachments(loader=lambda key: reads.append(key))
    page = main_window.material_library
    page.window_host.view.scene.attachments = store
    for i in range(300):
        store._meta[i] = ('preview.png','image/png')
        assert page._card_thumbnail({'preview':{'thumbnailAttachmentId':i}}) is None
    assert not reads


def test_card_thumbnail_is_decoded_off_the_gui_thread(main_window, qapp, qtbot,
                                                      tmp_path):
    """A card must never be decoded by the thread that draws the cards.

    Decoding every record of a page while the window was building it froze
    the GUI for six seconds on a 100k project. The card surface asks for the
    pictures it actually shows, and they arrive from the background loader.
    """
    import threading
    from unittest.mock import patch
    from PIL import Image
    from PyQt6 import QtGui
    from prism.material_textures import render_signature

    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library
    image = tmp_path/'preview.png'
    Image.new('RGB', (256, 256), 'blue').save(image)
    material = page.records.create()
    page.records.save_preview(material['id'], render_signature(material),
                               image.read_bytes())
    record = page.records.get(material['id'])
    assert isinstance(record['preview'].get('thumbnailAttachmentId'), int)

    real_from_data = QtGui.QImage.fromData
    threads = []
    def from_data(*args, **kwargs):
        threads.append(threading.get_ident())
        return real_from_data(*args, **kwargs)

    with patch('prism.widgets.material_cards.QtGui.QImage.fromData',
               side_effect=from_data):
        # Building the page of cards must not decode a single picture.
        page.materials.set_records(
            page.records.list_card_records(page.folder_id, ''),
            page.selected_id, page._card_thumbnail)
        assert not threads, 'a card was decoded on the GUI thread'

        # The background loader decodes it, off the GUI thread.
        page._thumbnail_cache.cache.clear()
        page._thumbnail_pending.clear()
        page._load_card_thumbnail(record['id'])
        qtbot.waitUntil(lambda: not page._jobs, timeout=15000)
        assert threads, 'the card was never decoded'
        assert all(thread != threading.get_ident() for thread in threads)

    # The loader cached the encoded picture, so the card surface gets it
    # without decoding again.
    with patch('prism.widgets.material_cards.QtGui.QImage.fromData',
               side_effect=from_data):
        decoded_before = len(threads)
        thumbnail = page._card_thumbnail(record)
        assert len(threads) == decoded_before, 'the cached card was decoded again'
        assert thumbnail and thumbnail.startswith('data:image/png')


def test_thumbnail_cache_peek_never_loads_and_reads_the_cache(tmp_path):
    from PIL import Image
    from prism.material_cache import MaterialThumbnailCache
    from prism.widgets.material_cards import material_thumbnail

    a = tmp_path/'a.png'
    Image.new('RGB', (256, 256), 'red').save(a)
    record = {'preview': {'thumbnailAttachmentId': 1}}
    attachments = {1: ('preview.png', 'image/png', a.read_bytes())}
    cache = MaterialThumbnailCache()
    # Nothing is cached yet, so a peek has to answer without loading.
    assert cache.peek(record, attachments) is None
    first = cache.get(record, attachments, material_thumbnail)
    assert first
    assert cache.peek(record, attachments) == first


def test_discarded_adjustments_do_not_revive_a_cached_card(main_window, qapp,
                                                            qtbot, tmp_path):
    """A discarded adjustment must not bring the old picture back.

    The cached thumbnail is keyed by the attachment's bytes, so it survives
    the adjustment being thrown away. Only the rule in the library keeps the
    stale picture off the card.
    """
    from PIL import Image
    from prism.material_textures import render_signature

    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library
    image = tmp_path/'preview.png'
    Image.new('RGB', (256, 256), 'blue').save(image)
    material = page.records.create()
    page.records.save_preview(material['id'], render_signature(material),
                               image.read_bytes())
    record = page.records.get(material['id'])

    page._load_card_thumbnail(record['id'])
    qtbot.waitUntil(lambda: not page._jobs, timeout=15000)
    assert page._card_thumbnail(record)

    discarded = dict(record)
    discarded['preview'] = dict(record['preview'], discardedAdjustments=True)
    assert page._card_thumbnail(discarded) is None


def test_thumbnail_cache_is_safe_to_fill_from_a_worker(main_window, qapp,
                                                        qtbot, tmp_path):
    """The GUI thread reads the cache while the loader fills it."""
    import threading
    from PIL import Image
    from prism.fileio.attachments_store import StoredAttachments
    from prism.material_cache import MaterialThumbnailCache
    from prism.widgets.material_cards import material_thumbnail

    a = tmp_path/'a.png'
    Image.new('RGB', (256, 256), 'red').save(a)
    payload = a.read_bytes()
    store = StoredAttachments(loader=lambda key: payload)
    store[1] = ('preview.png', 'image/png', payload)
    record = {'preview': {'thumbnailAttachmentId': 1}}
    cache = MaterialThumbnailCache()
    failures = []

    def fill():
        try:
            for _ in range(200):
                cache.get(record, store, material_thumbnail)
                cache.peek(record, store)
        except Exception as error:      # noqa: BLE001 - reported below
            failures.append(error)

    worker = threading.Thread(target=fill)
    worker.start()
    for _ in range(200):
        cache.peek(record, store)
    worker.join()
    assert not failures


def test_background_source_reads_do_not_pin_clean_originals():
    from prism.fileio.attachments_store import StoredAttachments
    from prism.material_textures import read_original
    store = StoredAttachments(loader=lambda key: b'persisted-original')
    store._meta[1] = ('source.png','image/png')
    assert read_original(store,1)[2] == b'persisted-original'
    assert store.loaded_count() == 0
    store[1] = ('source.png','image/png',b'new-user-import')
    assert read_original(store,1)[2] == b'new-user-import'


def test_16bit_displacement_display_is_normalized_not_clipped(tmp_path):
    import struct
    from prism.material_textures import prepare_texture, selected_image
    values = (0, 16384, 32768, 49152, 65535)
    source = tmp_path/'height16.png'
    Image.frombytes('I;16',(5,1), struct.pack('<5H',*values)).save(source)
    original = source.read_bytes()
    info, blob = prepare_texture(source)
    assert blob == original
    assert list(selected_image(blob,'R').tobytes()) == [0,63,127,191,255]
    assert info['grayscale']
