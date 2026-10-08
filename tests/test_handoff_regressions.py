"""Regression coverage for the two 2026-10-08 Prism handoffs."""
import json
import sqlite3
import pytest
from unittest.mock import MagicMock

from PyQt6 import QtCore, QtGui

from prism import fileio
from prism import commands
from prism.actions.ai_tag_workflow import TaggingWorker, apply_results
from prism.actions.export_workflow import export_images
from prism.fileio.sql import SQLiteIO
from prism.items import PrismPixmapItem, PrismTextItem


def image(width=48, height=32):
    value = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32)
    value.fill(QtGui.QColor('red'))
    return value


def item_on(view, filename='missing.png'):
    item = PrismPixmapItem(image(), filename)
    view.scene.addItem(item)
    return item


def test_ai_results_visible_undoable_and_persisted(view, tmp_path):
    item = item_on(view)
    item._tags = ['manual']
    item.setSelected(True)
    view.undo_stack.setClean()
    apply_results(view, {'items': {item: {'tags': ['tree', 'manual']}}, 'total': 1})
    assert item.tags == ['manual', 'tree']
    assert 'tree' in view._detail_panel._tag_checks
    assert not view.undo_stack.isClean()
    view.undo_stack.undo()
    assert item.tags == ['manual']
    view.undo_stack.redo()
    target = str(tmp_path / 'tagged.prism')
    database = SQLiteIO(target, view.scene, create_new=True)
    database.write()
    database._close_connection()
    view.scene.clear()
    database = SQLiteIO(target, view.scene, readonly=True)
    database.read()
    database._close_connection()
    view.scene.add_queued_items()
    loaded = list(view.scene.items_for_save())
    assert loaded[0].tags == ['manual', 'tree']


def test_tag_worker_emits_results_and_progress(qtbot, qapp, monkeypatch, tmp_path):
    source = tmp_path / 'stored.png'
    image().save(str(source))
    item = fileio._load_image_item(str(source))
    fake = MagicMock()
    fake.load_model.return_value = True
    fake.tag_image.return_value = [('tree', 0.9)]
    monkeypatch.setattr('prism.ai_inference.InferenceProcess', lambda **kwargs: fake)
    worker = TaggingWorker([item])
    results, progress, done = [], [], []
    worker.results_ready.connect(results.append)
    worker.progress.connect(progress.append)
    worker.finished.connect(lambda *args: done.append(args))
    worker.start()
    qtbot.waitUntil(lambda: bool(done), timeout=5000)
    assert worker.wait(5000)
    assert results[0]['items'][item]['tags'] == ['tree']
    assert progress == [1]
    assert item._source_blob is None
    fake.close.assert_called_once()


def test_tag_worker_failure_finishes_without_losing_manual_tags(qtbot, qapp, monkeypatch):
    item = PrismPixmapItem(image(), 'missing.png')
    item._tags = ['manual']
    monkeypatch.setattr('prism.ai_inference.InferenceProcess',
                        MagicMock(side_effect=RuntimeError('native runtime failed')))
    worker = TaggingWorker([item])
    results = []
    worker.results_ready.connect(results.append)
    worker.start()
    qtbot.waitUntil(lambda: bool(results), timeout=5000)
    worker.wait(5000)
    assert results[0]['errors'] == ['native runtime failed']
    assert item.tags == ['manual']


def test_empty_selected_export_does_not_open_dialogs_or_export_all(view, monkeypatch):
    item_on(view)
    choose = MagicMock()
    monkeypatch.setattr(view, '_ask_export_mode', choose)
    export_images(view, selected_only=True)
    choose.assert_not_called()


def test_text_only_selected_export_is_handled(view, monkeypatch):
    text = PrismTextItem('note')
    view.scene.addItem(text)
    text.setSelected(True)
    choose = MagicMock()
    monkeypatch.setattr(view, '_ask_export_mode', choose)
    export_images(view, selected_only=True)
    choose.assert_not_called()


def test_selected_export_writes_only_selected_original(view, qtbot, monkeypatch, tmp_path):
    one = tmp_path / 'one.png'
    two = tmp_path / 'two.png'
    image().save(str(one))
    image().save(str(two))
    first, second = [fileio._load_image_item(str(path)) for path in (one, two)]
    view.scene.addItem(first)
    view.scene.addItem(second)
    first.setSelected(True)
    monkeypatch.setattr(view, '_ask_export_mode', lambda: 'original')
    destination = tmp_path / 'export'
    destination.mkdir()
    monkeypatch.setattr('PyQt6.QtWidgets.QFileDialog.getExistingDirectory',
                        lambda **kwargs: str(destination))
    export_images(view, selected_only=True)
    assert view.worker.wait(5000)
    qtbot.wait(50)
    files = list(destination.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == one.read_bytes()


def test_large_import_arranges_new_items_per_canvas_without_moving_existing(view):
    existing = item_on(view)
    existing.setPos(9000, 7000)
    view._existing_item_ids = {id(existing)}
    new = []
    for index in range(502):
        item = item_on(view, f'{index}.png')
        item._canvas_id = 'a' if index < 251 else 'b'
        new.append(item)
    # Folder import assigns canvas membership during the completion callback.
    view._pending_folder_trees = {'root': 'root'}
    def assign(_trees, items):
        for index, item in enumerate(items):
            item._canvas_id = 'a' if index < 251 else 'b'
    view._create_canvases_for_folders = assign
    view.undo_stack.beginMacro('import')
    view.on_insert_images_finished(False, '', [])
    for canvas in ('a', 'b'):
        group = [item for item in new if item._canvas_id == canvas]
        assert len({(item.x(), item.y()) for item in group}) == len(group)
    assert existing.pos() == QtCore.QPointF(9000, 7000)
    assert view._existing_item_ids == set()


def test_unsaved_import_keeps_original_after_file_deleted(qapp, tmp_path):
    path = tmp_path / 'original.png'
    image(1600, 900).save(str(path))
    original = path.read_bytes()
    item = fileio._load_image_item(str(path))
    assert not item.has_full_pixmap()
    assert item._source_blob is None
    assert item._image_size == QtCore.QSize(1600, 900)
    path.unlink()
    assert item.original_bytes() == original


def test_zoom_and_full_export_rehydrate_released_original(qapp, tmp_path):
    path = tmp_path / 'original.png'
    image(1600, 900).save(str(path))
    item = fileio._load_image_item(str(path))
    assert item.ensure_full_pixmap()
    assert item.release_full_pixmap()
    assert item.release_source_blob()
    target = image(1600, 900)
    painter = QtGui.QPainter(target)
    try:
        pixmap, _ = item._pixmap_for_painting(painter)
        assert pixmap.size() == QtCore.QSize(1600, 900)
    finally:
        painter.end()
    assert item.original_bytes() == path.read_bytes()


def test_save_as_rebinds_deferred_source_to_new_archive(view, tmp_path):
    item_on(view)
    first, second = [str(tmp_path / name) for name in ('one.prism', 'two.prism')]
    database = SQLiteIO(first, view.scene, create_new=True)
    database.write()
    database._close_connection()
    view.scene.clear()
    database = SQLiteIO(first, view.scene, readonly=True)
    database.read()
    database._close_connection()
    view.scene.add_queued_items()
    item = list(view.scene.items_for_save())[0]
    assert item._source_blob is None
    original = item.original_bytes()
    item._source_blob = None
    database = SQLiteIO(second, view.scene, create_new=True)
    database.write()
    database._close_connection()
    from pathlib import Path
    Path(first).unlink()
    assert item._source_blob is None
    assert item.original_bytes() == original


def test_metadata_query_does_not_materialize_images_for_colour(view, tmp_path):
    item_on(view)
    target = str(tmp_path / 'metadata.prism')
    database = SQLiteIO(target, view.scene, create_new=True)
    database.write()
    database._close_connection()
    view.scene.clear()
    database = SQLiteIO(target, view.scene, readonly=True)
    database.read()
    database._close_connection()
    view.scene.add_queued_items()
    item = list(view.scene.items_for_save())[0]
    assert item._source_blob is None
    assert not item.has_full_pixmap()


def test_legacy_geometry_is_valid_before_first_paint(view, tmp_path):
    item_on(view)
    target = str(tmp_path / 'legacy.prism')
    database = SQLiteIO(target, view.scene, create_new=True)
    database.write()
    database._close_connection()
    with sqlite3.connect(target) as connection:
        identifier, metadata = connection.execute('SELECT id, data FROM items').fetchone()
        metadata = json.loads(metadata)
        metadata.pop('imageWidth')
        metadata.pop('imageHeight')
        connection.execute('UPDATE items SET data=? WHERE id=?',
                           (json.dumps(metadata), identifier))
    view.scene.clear()
    database = SQLiteIO(target, view.scene, readonly=True)
    database.read()
    database._close_connection()
    view.scene.add_queued_items()
    item = list(view.scene.items_for_save())[0]
    assert item._image_size == QtCore.QSize(48, 32)
    assert not item.crop.isEmpty()
    assert item._source_blob is None


def test_bulk_insert_restores_signal_and_viewport_states(view):
    view.scene.blockSignals(True)
    view.viewport().setUpdatesEnabled(False)
    view.scene.add_item_later({'type': 'pixmap', 'item': PrismPixmapItem(image())})
    view.scene.add_queued_items()
    assert view.scene.signalsBlocked()
    assert not view.viewport().updatesEnabled()
    view.scene.blockSignals(False)
    view.viewport().setUpdatesEnabled(True)


def test_delete_save_undo_preserves_the_original_and_can_be_saved_again(view, tmp_path):
    item_on(view)
    target = str(tmp_path / 'undo.prism')
    database = SQLiteIO(target, view.scene, create_new=True)
    database.write()
    database._close_connection()
    item = list(view.scene.items_for_save())[0]
    original = item.original_bytes()
    item._source_blob = None
    view.undo_stack.push(commands.DeleteItems(view.scene, [item]))
    database = SQLiteIO(target, view.scene)
    database.write()
    database._close_connection()
    view.undo_stack.undo()
    assert item.original_bytes() == original
    database = SQLiteIO(target, view.scene)
    database.write()
    assert database.fetchone('SELECT COUNT(*) FROM items')[0] == 1
    assert database.fetchone('SELECT data FROM sqlar')[0] == original
    database._close_connection()


def test_destructive_pixel_replacement_cannot_reuse_old_source(qapp, tmp_path):
    path = tmp_path / 'old.png'
    image().save(str(path))
    item = fileio._load_image_item(str(path))
    changed = image(60, 40)
    changed.fill(QtGui.QColor('blue'))
    item.setPixmap(QtGui.QPixmap.fromImage(changed))
    assert item.original_bytes() is None
    assert item.pixmap().size() == QtCore.QSize(60, 40)


def test_import_painting_uses_preview_until_batch_finishes(view, tmp_path):
    path = tmp_path / 'large.png'
    image(1600, 900).save(str(path))
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    view._import_in_progress = True
    target = image(1600, 900)
    painter = QtGui.QPainter(target)
    try:
        pixmap, _ = item._pixmap_for_painting(painter)
        assert pixmap is item._preview_pixmap
        assert not item.has_full_pixmap()
        assert item._source_blob is None
    finally:
        painter.end()
        view._import_in_progress = False


def test_archived_video_and_model_rows_are_not_dropped(view, tmp_path, monkeypatch):
    target = str(tmp_path / 'media.prism')
    database = SQLiteIO(target, view.scene, create_new=True)
    database.create_schema_on_new()
    for kind in ('video', 'glb'):
        database.ex('INSERT INTO items (type,x,y,z,scale,rotation,flip,data) '
                    'VALUES (?,0,0,0,1,0,1,?)',
                    (kind, json.dumps({'filename': f'stored.{kind}'})))
        database.ex('INSERT INTO sqlar (item_id,data) VALUES (?,?)',
                    (database.cursor.lastrowid, b'embedded-source'))
    database.connection.commit()
    database._close_connection()
    video, model = MagicMock(), MagicMock()
    monkeypatch.setattr('prism.fileio.sql.PrismVideoItem', video)
    monkeypatch.setattr('prism.fileio.sql.PrismGlbItem', model)
    database = SQLiteIO(target, view.scene, readonly=True)
    database.read()
    database._close_connection()
    assert video.call_count == 1
    assert model.call_count == 1
    assert view.scene.items_to_add.qsize() == 2


def test_canceled_save_does_not_delete_unvisited_existing_items(view, tmp_path):
    item_on(view)
    item_on(view)
    target = str(tmp_path / 'canceled.prism')
    database = SQLiteIO(target, view.scene, create_new=True)
    database.write()
    database._close_connection()
    worker = MagicMock(canceled=True)
    database = SQLiteIO(target, view.scene, worker=worker)
    database.write()
    assert database.fetchone('SELECT COUNT(*) FROM items')[0] == 2
    assert database.fetchone('SELECT COUNT(*) FROM sqlar')[0] == 2
    assert worker.finished.emit.call_args.args[1]
    database._close_connection()


def test_export_preflight_does_not_retain_all_original_bytes(qapp, tmp_path):
    from prism.export_service import ExportService
    path = tmp_path / 'large.png'
    image(1600, 900).save(str(path))
    items = [fileio._load_image_item(str(path)) for _ in range(10)]
    assert ExportService.check_original(items).all_ready
    assert all(item._source_blob is None for item in items)


def test_native_inference_exit_is_reported_without_exiting_host(monkeypatch):
    import subprocess
    import sys
    from prism.ai_inference import InferenceProcess
    real_popen = subprocess.Popen
    def failing_child(_command, **kwargs):
        return real_popen([sys.executable, '-c', 'import os; os._exit(42)'], **kwargs)
    monkeypatch.setattr('prism.ai_inference.subprocess.Popen', failing_child)
    process = InferenceProcess()
    try:
        with pytest.raises(RuntimeError):
            process.tag_image('unused.png')
    finally:
        process.close()


def test_background_preview_keeps_exif_orientation(qapp, tmp_path, monkeypatch):
    import io
    import threading
    from PIL import Image
    from prism import thumbnail_cache
    picture = Image.new('RGB', (80, 40), 'red')
    exif = picture.getexif()
    exif[274] = 6
    output = io.BytesIO()
    picture.save(output, format='JPEG', exif=exif)
    data = output.getvalue()
    cache = thumbnail_cache.ThumbnailCache(directory=str(tmp_path))
    monkeypatch.setattr(thumbnail_cache, 'cache', lambda: cache)
    key = thumbnail_cache.digest(data)
    thread = threading.Thread(target=thumbnail_cache._build_previews, args=([(key, data)],))
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    preview = cache.load(key)
    assert preview.size == QtCore.QSize(40, 80)
    assert preview.image.width() < preview.image.height()


def test_generated_canvas_image_can_be_tagged_without_inventing_original(view, qtbot, monkeypatch):
    from prism.actions.ai_tag_workflow import start_tagging
    item = item_on(view, None)
    local_client = MagicMock()
    local_client.is_local_only_mode.return_value = True
    monkeypatch.setattr('prism.ai_client.DeepSeekClient', lambda: local_client)
    fake = MagicMock()
    fake.load_model.return_value = True
    fake.tag_image.return_value = [('red', 0.9)]
    monkeypatch.setattr('prism.ai_inference.InferenceProcess', lambda **kwargs: fake)
    start_tagging(view, [item])
    qtbot.waitUntil(lambda: 'red' in item.tags, timeout=5000)
    view.worker.wait(5000)
    assert fake.tag_image.call_args.args[0].getvalue().startswith(b'\x89PNG')
    assert item.original_bytes() is None


def test_cloud_category_suggestion_is_visible_in_notes_and_undoable(view):
    item = item_on(view)
    item._notes = 'manual note'
    apply_results(view, {'items': {item: {'tags': ['tree'],
                   'category_suggestion': 'landscape'}}, 'total': 1})
    assert item.notes == 'manual note\nAI 分类建议：landscape'
    view.undo_stack.undo()
    assert item.notes == 'manual note'


def test_rebuilding_current_project_preserves_deferred_originals(view, tmp_path):
    item_on(view)
    target = str(tmp_path / 'rebuild.prism')
    database = SQLiteIO(target, view.scene, create_new=True)
    database.write()
    database._close_connection()
    item = list(view.scene.items_for_save())[0]
    original = item.original_bytes()
    item._source_blob = None
    database = SQLiteIO(target, view.scene, create_new=True)
    database.write()
    assert database.fetchone('SELECT data FROM sqlar')[0] == original
    database._close_connection()
