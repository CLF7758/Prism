"""Large boards retain logical assets while Qt editing objects are recycled."""
import hashlib
from unittest.mock import Mock
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism import asset_records, commands, fileio, lod
from prism.asset_records import ImageRecord
from prism.fileio.sql import SQLiteIO
from prism.item_lifecycle import is_deleted, is_record
from prism.items import PrismPixmapItem, PrismTextItem
from prism.project_catalog import ProjectAssetCatalog


def record(view, tmp_path, color='red', x=0, z=0, extra=None):
    image = QtGui.QImage(100, 80, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(color))
    path = tmp_path/f'{color}.png'
    assert image.save(str(path))
    item = ImageRecord({'x': x, 'z': z, 'data': {'imageWidth': 100,
        'imageHeight': 80, 'filename': str(path), **(extra or {})}}, None)
    item._source_path = str(path)
    item._preview_pixmap = QtGui.QPixmap.fromImage(image)
    view.scene.addItem(item)
    return item


def test_virtual_scene_does_not_put_recycled_editors_in_qt_bsp(view, tmp_path):
    assert view.scene.itemIndexMethod() == QtWidgets.QGraphicsScene.ItemIndexMethod.BspTreeIndex
    item = record(view, tmp_path)
    assert view.scene.itemIndexMethod() == QtWidgets.QGraphicsScene.ItemIndexMethod.NoIndex
    native = PrismTextItem('mixed native item')
    view.scene.addItem(native)
    manager = view.scene._viewport_items
    for _ in range(30):
        manager.materialize(item)
        manager.rebuild_layers()
        assert item in view.scene.items(item.sceneBoundingRect(), QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect)
        assert native in view.scene.items()
        manager.recycle(item)
        manager.rebuild_layers()
        assert item in view.scene.items(item.sceneBoundingRect(), QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect)


def test_dense_selection_reconciles_editors_without_moving_camera(view, tmp_path):
    seed = record(view, tmp_path)
    items = [seed]
    for _ in range(599):
        item = ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 80}}, None)
        item._source_path = seed._source_path
        item._preview_pixmap = seed._preview_pixmap
        view.scene.addItem(item)
        items.append(item)
    view.centerOn(50, 40)
    manager = view.scene._viewport_items
    manager.sync(view)
    assert not manager.materialized
    for item in items[:25]:
        item.setSelected(True)
    for _ in range(10):
        manager.sync(view)
        if len(manager.materialized) == 25:
            break
    assert manager.materialized == set(items[:25])
    assert set(view.scene.selectedItems(user_only=True)) == set(items[:25])


def test_records_do_not_create_graphics_items_until_visible(view, tmp_path):
    first = record(view, tmp_path)
    second = record(view, tmp_path, 'blue', 100000)
    assert not isinstance(first, QtWidgets.QGraphicsItem)
    assert first._graphics is second._graphics is None
    assert len(list(view.scene.items_for_save())) == 2
    assert ProjectAssetCatalog(view.scene).total_count() == 2
    view.centerOn(first)
    manager = view.scene._viewport_items
    manager.sync(view)
    assert first._graphics is not None
    assert second._graphics is None
    first.tags = ['kept']
    first._graphics.moveBy(10, 20)
    position = first.pos()
    original_graphics = first._graphics
    view.centerOn(second)
    manager.sync(view)
    assert first._graphics is None
    assert sip.isdeleted(original_graphics)
    assert first.pos() == position
    assert first.tags == ['kept']
    assert not is_deleted(first)
    view.centerOn(first)
    manager.sync(view)
    assert first._graphics is not original_graphics
    assert first.pos() == position


def test_save_and_save_as_include_offscreen_records_without_materializing(view, tmp_path, monkeypatch):
    monkeypatch.setattr(asset_records, 'MIN_VIRTUAL_IMAGES', 1)
    first = record(view, tmp_path, extra={'tags': ['test'], 'unknownExtension': {'value': 7}})
    second = record(view, tmp_path, 'blue', 100000)
    originals = [hashlib.sha256(i.original_bytes()).hexdigest() for i in (first, second)]
    path = tmp_path/'all.prism'
    writer = SQLiteIO(str(path), view.scene, create_new=True)
    writer.write(); writer._close_connection()
    assert first._graphics is second._graphics is None
    view.scene.clear()
    reader = SQLiteIO(str(path), view.scene, readonly=True)
    reader.read(); reader._close_connection()
    view.scene.add_queued_items()
    loaded = list(view.scene.items_for_save())
    assert len(loaded) == 2 and all(is_record(i) for i in loaded)
    assert [hashlib.sha256(i.original_bytes()).hexdigest() for i in loaded] == originals
    assert loaded[0].get_extra_save_data()['unknownExtension'] == {'value': 7}
    loaded[0].tags = ['changed']
    second_path = tmp_path/'save-as.prism'
    writer = SQLiteIO(str(second_path), view.scene, create_new=True)
    writer.write(); writer._close_connection()
    assert all(i._graphics is None for i in loaded)
    assert [hashlib.sha256(i.original_bytes()).hexdigest() for i in loaded] == originals


def test_regular_save_updates_positions_without_rebuilding_archive(view, tmp_path):
    item = record(view, tmp_path)
    path = tmp_path/'incremental.prism'
    writer = SQLiteIO(str(path), view.scene, create_new=True)
    writer.write()
    writer._close_connection()
    digest = hashlib.sha256(item.original_bytes()).hexdigest()
    item.setPos(123, 456)
    writer = SQLiteIO(str(path), view.scene)
    statements = []
    writer.connection.set_trace_callback(statements.append)
    writer.write()
    assert writer.fetchone('SELECT x,y FROM items WHERE id=?', (item.save_id,)) == (123, 456)
    assert not any(sql.lstrip().upper().startswith('VACUUM') for sql in statements)
    assert hashlib.sha256(item.original_bytes()).hexdigest() == digest
    writer._close_connection()


def test_metadata_transform_trash_and_permanent_delete_are_undoable(view, tmp_path):
    item = record(view, tmp_path)
    stack = view.undo_stack
    stack.push(commands.MoveItemsBy([item], QtCore.QPointF(33, 44)))
    assert item.pos() == QtCore.QPointF(33, 44)
    stack.undo(); assert item.pos() == QtCore.QPointF()
    stack.redo(); assert item.pos() == QtCore.QPointF(33, 44)
    stack.push(commands.ChangeMetadata([item], 'tags', [['new']]))
    assert item.tags == ['new']
    stack.undo(); assert item.tags == []
    stack.push(commands.TrashItems(view.scene, [item]))
    assert not item.isVisible() and item in list(view.scene.items_for_save())
    stack.undo(); assert item.isVisible()
    stack.push(commands.DeleteItems(view.scene, [item]))
    assert item not in list(view.scene.items_for_save())
    stack.undo(); assert item in list(view.scene.items_for_save())
    assert item._graphics is None


@pytest.mark.parametrize('flip', [1, -1])
@pytest.mark.parametrize('rotation', [0, 45, 90])
def test_geometry_matches_native_qt_before_and_after_recycling(view, tmp_path, flip, rotation):
    item = record(view, tmp_path)
    item.setPos(-120, 55)
    item.setScale(.7)
    item.setRotation(rotation)
    if flip < 0:
        item.do_flip()
    before = item.sceneTransform()
    geometry = item.sceneBoundingRect()
    graphics = item.graphics_item()
    assert item.sceneTransform() == before
    assert item.sceneBoundingRect() == geometry
    view.scene._viewport_items.reconciling = True
    try:
        view.scene._viewport_items.recycle(item)
    finally:
        view.scene._viewport_items.reconciling = False
    assert item.sceneTransform() == before
    assert item.sceneBoundingRect() == geometry


def render(scene, rect):
    image = QtGui.QImage(100, 80, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor('white'))
    painter = QtGui.QPainter(image)
    try:
        scene.render(painter, QtCore.QRectF(image.rect()), rect)
    finally:
        painter.end()
    return image


def test_export_preserves_stacking_with_native_objects_and_hidden_records(view, tmp_path):
    red = record(view, tmp_path, 'red', z=0)
    green = PrismPixmapItem(QtGui.QImage(100, 80, QtGui.QImage.Format.Format_ARGB32))
    green.setPixmap(QtGui.QPixmap(100, 80))
    green.pixmap().fill(QtGui.QColor('green'))
    pixmap = QtGui.QPixmap(100, 80); pixmap.fill(QtGui.QColor('green'))
    green.setPixmap(pixmap)
    green.setZValue(1)
    view.scene.addItem(green)
    blue = record(view, tmp_path, 'blue', z=2)
    assert render(view.scene, QtCore.QRectF(0, 0, 100, 80)).pixelColor(50, 40) == QtGui.QColor('blue')
    blue.setVisible(False)
    assert render(view.scene, QtCore.QRectF(0, 0, 100, 80)).pixelColor(50, 40) == QtGui.QColor('green')
    green.setVisible(False)
    assert render(view.scene, QtCore.QRectF(0, 0, 100, 80)).pixelColor(50, 40) == QtGui.QColor('red')
    assert red._graphics is blue._graphics is None


def test_selection_hit_testing_and_clear_use_logical_records(view, tmp_path):
    first = record(view, tmp_path)
    second = record(view, tmp_path, 'blue', 1000)
    assert view.scene.itemAt(QtCore.QPointF(50, 40), QtGui.QTransform()) is first
    view.scene.select_all_items()
    assert set(view.scene.selectedItems(user_only=True)) == {first, second}
    assert first._graphics is second._graphics is None
    view.scene.clearSelection()
    assert not view.scene.selectedItems(user_only=True)
    view.scene.clear()
    assert is_deleted(first) and is_deleted(second)


def test_large_import_builds_records_on_io_side_without_pixmap_allocation(view, tmp_path, monkeypatch):
    path = tmp_path/'source.png'
    image = QtGui.QImage(100, 80, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red')); assert image.save(str(path))
    monkeypatch.setattr(asset_records, 'MIN_VIRTUAL_IMAGES', 1)
    worker = Mock(canceled=False)
    fileio.load_media([str(path)], QtCore.QPointF(), view.scene, worker)
    data, _selected = view.scene.items_to_add.get_nowait()
    item = data['item']
    assert is_record(item) and item._graphics is None
    assert item.original_bytes() == path.read_bytes()


def test_native_crop_and_transform_commands_survive_recycling(view, tmp_path):
    item = record(view, tmp_path)
    graphics = item.graphics_item()
    stack = view.undo_stack
    stack.push(commands.CropItem(graphics, QtCore.QRectF(10, 10, 50, 40)))
    stack.push(commands.RotateItemsBy([graphics], 45, item.center_scene_coords))
    view.scene._viewport_items.recycle(item)
    assert sip.isdeleted(graphics)
    stack.undo(); assert item.rotation() == 0
    stack.undo(); assert item.crop == QtCore.QRectF(0, 0, 100, 80)
    stack.redo(); assert item.crop == QtCore.QRectF(10, 10, 50, 40)
    assert item._graphics is None


def test_pinned_source_survives_staging_cleanup_and_repeated_reads(view, tmp_path):
    item = record(view, tmp_path)
    expected = item.original_bytes()
    item.materialize_source()
    from pathlib import Path
    Path(item._source_path).unlink()
    assert item.original_bytes() == expected
    assert item.original_bytes() == expected
    assert not item.release_source_blob()


def test_export_adjusted_pixels_and_copy_survive_recycling(view, tmp_path):
    item = record(view, tmp_path)
    graphics = item.graphics_item()
    adjusted = QtGui.QPixmap(100, 80); adjusted.fill(QtGui.QColor('blue'))
    graphics.set_preview_pixmap(adjusted)
    source = item.original_bytes()
    assert render(view.scene, QtCore.QRectF(0, 0, 100, 80)).pixelColor(50, 40) == QtGui.QColor('blue')
    copied = item.create_copy()
    view.scene._viewport_items.recycle(item)
    assert render(view.scene, QtCore.QRectF(0, 0, 100, 80)).pixelColor(50, 40) == QtGui.QColor('blue')
    assert copied._preview_adjusted and copied._derived_pixmap.toImage().pixelColor(50, 40) == QtGui.QColor('blue')
    assert item.original_bytes() == source


def test_active_and_passive_export_crop_gray_and_opacity_match(view, tmp_path):
    item = record(view, tmp_path)
    item.crop = QtCore.QRectF(10, 10, 50, 40)
    item.grayscale = True
    item.setOpacity(.5)
    passive = render(view.scene, item.crop)
    item.graphics_item()
    active = render(view.scene, item.crop)
    assert active == passive, (active.pixelColor(50, 40).getRgb(), passive.pixelColor(50, 40).getRgb())
    color = active.pixelColor(50, 40)
    assert color.red() == color.green() == color.blue()


def test_same_z_stacking_survives_late_materialization(view, tmp_path):
    item = record(view, tmp_path)
    overlay = QtWidgets.QGraphicsRectItem(0, 0, 100, 80)
    overlay.setBrush(QtGui.QColor('green')); overlay.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
    view.scene.addItem(overlay)
    item.graphics_item()
    assert render(view.scene, item.crop).pixelColor(50, 40) == QtGui.QColor('green')
    view.scene._viewport_items.recycle(item)
    assert render(view.scene, item.crop).pixelColor(50, 40) == QtGui.QColor('green')


def test_warm_view_does_not_repeat_spatial_scan(view, tmp_path, monkeypatch):
    item = record(view, tmp_path)
    view.centerOn(item)
    manager = view.scene._viewport_items
    manager.sync(view)
    query = Mock(wraps=manager.index.query)
    monkeypatch.setattr(manager.index, 'query', query)
    manager.sync(view); manager.sync(view)
    assert not query.called


def test_passive_display_decoder_does_not_create_editing_objects(view, tmp_path, qtbot):
    from prism import image_decode
    origin = record(view, tmp_path, 'blue')
    item = record(view, tmp_path, 'red', 100000)
    view.centerOn(origin)
    item._preview_pixmap = None
    image_decode.request_display(item, 192)
    qtbot.waitUntil(lambda: item._preview_pixmap is not None, timeout=5000)
    assert item._graphics is None
    assert item._preview_pixmap.toImage().pixelColor(50, 40) == QtGui.QColor('red')


def test_hiding_and_removing_selected_records_updates_selection(view, tmp_path, qtbot):
    item = record(view, tmp_path)
    item.setSelected(True)
    item.setVisible(False)
    assert not view.scene.selectedItems(user_only=True)
    item.setVisible(True); item.setSelected(True)
    with qtbot.waitSignal(view.scene.selectionChanged):
        view.scene.removeItem(item)
    assert not view.scene.selectedItems(user_only=True)


def test_zooming_selected_records_does_not_materialize_offscreen_assets(view, tmp_path):
    first = record(view, tmp_path)
    second = record(view, tmp_path, 'blue', 100000)
    view.scene.select_all_items()
    view.scene.on_view_scale_change()
    assert first._graphics is second._graphics is None


def test_pixel_export_from_worker_does_not_create_graphics_objects(view, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    item = record(view, tmp_path)
    item.crop = QtCore.QRectF(10, 10, 50, 40)
    item.grayscale = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        blob, _format = pool.submit(item.pixmap_to_bytes, True, True).result(timeout=5)
    image = QtGui.QImage.fromData(blob)
    assert image.size() == QtCore.QSize(50, 40)
    assert image.pixelColor(25, 20).red() == image.pixelColor(25, 20).green()
    assert item._graphics is None


def test_selection_add_contains_and_rotated_hit_tests_match_qt(view, tmp_path):
    first = record(view, tmp_path)
    second = record(view, tmp_path, 'blue', 1000)
    first.setRotation(45)
    corner = first.sceneBoundingRect().topLeft()
    path = QtGui.QPainterPath(); path.addRect(QtCore.QRectF(corner, QtCore.QSizeF(2, 2)))
    assert first not in view.scene.items(path)
    assert first in view.scene.items(path, QtCore.Qt.ItemSelectionMode.IntersectsItemBoundingRect)
    view.scene.setSelectionArea(path, mode=QtCore.Qt.ItemSelectionMode.ContainsItemShape)
    assert not first.isSelected()
    first.setSelected(True)
    path = QtGui.QPainterPath(); path.addRect(second.sceneBoundingRect().adjusted(-1, -1, 1, 1))
    view.scene.setSelectionArea(path, QtCore.Qt.ItemSelectionOperation.AddToSelection)
    assert set(view.scene.selectedItems(user_only=True)) == {first, second}
    assert first._graphics is second._graphics is None


def test_empty_archive_uses_original_file_and_never_replaces_good_archive(view, tmp_path):
    import sqlite3
    from prism import image_decode
    item = record(view, tmp_path)
    original = item.original_bytes()
    archive = tmp_path/'empty.prism'
    with sqlite3.connect(archive) as connection:
        connection.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        connection.execute('INSERT INTO sqlar VALUES(7, ?)', (b'',))
    item._prism_source = (str(archive), 7); item._source_path = None
    assert item.original_bytes() == original
    image, _size = image_decode.decode(*image_decode.source_snapshot(item), max_side=192)
    assert image.pixelColor(50, 40) == QtGui.QColor('red')
    with sqlite3.connect(archive) as connection:
        connection.execute('UPDATE sqlar SET data=?', (original,))
    # An archive payload takes precedence over a later edit of the external file.
    source = QtGui.QImage(100, 80, QtGui.QImage.Format.Format_RGB32)
    source.fill(QtGui.QColor('blue')); source.save(item.filename)
    assert item.original_bytes() == original


def test_failed_save_as_restores_ids_and_does_not_rebuild_original(view, tmp_path):
    import sqlite3
    from prism.fileio.errors import PrismFileIOError
    item = record(view, tmp_path)
    item.save_id = 17
    item._source_path = str(tmp_path/'missing.png')
    original = tmp_path/'existing.prism'
    with sqlite3.connect(original) as connection:
        connection.execute('CREATE TABLE sentinel(value TEXT)')
        connection.execute("INSERT INTO sentinel VALUES('keep')")
    before = original.read_bytes()
    writer = SQLiteIO(str(tmp_path/'new.prism'), view.scene, create_new=True)
    with pytest.raises(PrismFileIOError, match='Original image is missing'):
        writer.write()
    assert item.save_id == 17
    assert not writer.retry
    assert original.read_bytes() == before
    assert not (tmp_path/'new.prism.bak').exists()


def test_overview_group_selection_keeps_records_without_editors(view, tmp_path):
    first = record(view, tmp_path)
    others = [ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 80}}, None) for _ in range(20)]
    for item in others:
        view.scene.addItem(item)
    for item in [first, *others]:
        item.setSelected(True)
    view._overview_active = True
    view.scene._viewport_items.sync(view)
    assert not view.scene._viewport_items.materialized
    assert set(view.scene._viewport_items.selected) == {first, *others}
