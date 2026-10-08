"""Regressions for the program audit; only isolated scenes and files are used."""


from PyQt6 import QtCore, QtGui
import pytest
from prism.items import PrismPixmapItem
from prism.project_search import search_project


def populate(window):
    scene = window.view.scene
    scene.workspace_pages = [
        {'id': 'default-canvas', 'kind': 'canvas', 'title': 'Old'},
        {'id': 'new-canvas', 'kind': 'canvas', 'title': 'New'}]
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image, filename='audit-unique-image.png')
    scene.addItem(item)
    panel = window.view.category_panel
    panel.rebuild_workspace_tree()
    service = panel.resource_service
    node = next(n for n in service.nodes if n.get('pageId') == 'default-canvas')
    service.trash([node['id']], '2026-10-08T00:00:00+00:00')
    return item, node, service


def test_startup_does_not_choose_trashed_canvas(main_window):
    populate(main_window)
    assert main_window._first_canvas_id() == 'new-canvas'


def test_permanent_purge_removes_canvas_images(main_window):
    item, node, service = populate(main_window)
    service.purge([node['id']])
    main_window._on_trash_purged(['default-canvas'])
    assert item not in list(main_window.view.scene.items_for_save())


def test_search_excludes_images_in_trashed_canvas(main_window):
    item, node, service = populate(main_window)
    results = search_project(main_window.view.scene, service, 'audit-unique-image')
    assert not any(result.get('item') is item for result in results)


@pytest.mark.parametrize('rgb', [(255, 0, 0), (0, 255, 0), (0, 0, 255), (180, 40, 90)])
def test_grayscale_sample_matches_displayed_pixels(view, rgb):
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(*rgb))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    item.grayscale = True
    canvas = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
    canvas.fill(QtGui.QColor('transparent'))
    painter = QtGui.QPainter(canvas)
    view.scene.render(painter, QtCore.QRectF(0, 0, 40, 40),
                      QtCore.QRectF(0, 0, 40, 40))
    painter.end()
    shown = canvas.pixelColor(20, 20)
    sampled = item.sample_color_at(QtCore.QPointF(20, 20))
    assert sampled == shown, (sampled.getRgb(), shown.getRgb())
    exported_bytes, _ = item.pixmap_to_bytes(apply_grayscale=True)
    exported = QtGui.QImage.fromData(exported_bytes)
    assert exported.pixelColor(20, 20) == shown


def test_svg_export_excludes_hidden_canvas_images(view):
    from prism.fileio.export import SceneToSVGExporter
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor('red'))
    for index in range(2):
        item = PrismPixmapItem(image)
        item.setPos(index * 10000, 0)
        view.scene.addItem(item)
        item.setVisible(index == 0)
    root = SceneToSVGExporter(view.scene).render_to_svg()
    assert len(list(root.iter('image'))) == 1


def test_image_export_size_excludes_hidden_canvases(view):
    from prism.fileio.export import SceneToPixmapExporter
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor('red'))
    for index in range(2):
        item = PrismPixmapItem(image)
        item.setPos(index * 10000, 0)
        view.scene.addItem(item)
        item.setVisible(index == 0)
    exporter = SceneToPixmapExporter(view.scene)
    assert exporter.default_size.width() < 100


def test_manual_opacity_edit_is_saved_while_color_filter_active(main_window):
    from prism.commands import ChangeOpacity
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    item.color_group = 'red'
    main_window.view.scene.addItem(item)
    main_window.color_filter.apply_filter('red')
    main_window.view.undo_stack.push(ChangeOpacity([item], 0.5))
    assert item.get_extra_save_data()['opacity'] == 0.5


def test_intentional_30_percent_opacity_survives_filter(main_window):
    images = []
    for color, opacity in [('red', 0.3), ('blue', 1.0)]:
        image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(color))
        item = PrismPixmapItem(image)
        item.color_group = color
        item.setOpacity(opacity)
        main_window.view.scene.addItem(item)
        images.append(item)
    main_window.color_filter.apply_filter('blue')
    main_window.color_filter.clear_filter()
    assert images[0].opacity() == 0.3


def test_restoring_trashed_canvas_restores_navigation_and_search(main_window):
    item, node, service = populate(main_window)
    service.restore_deleted([node['id']])
    main_window.view.category_panel.rebuild_workspace_tree()
    assert main_window._first_canvas_id() == 'default-canvas'
    main_window.open_workspace_page('canvas', 'default-canvas')
    assert item.isVisible()
    assert any(result.get('item') is item for result in
               search_project(main_window.view.scene, service, 'audit-unique-image'))


def test_deleting_last_canvas_shows_empty_canvas_without_losing_items(main_window):
    item, node, service = populate(main_window)
    new_node = next(n for n in service.nodes if n.get('pageId') == 'new-canvas')
    service.trash([new_node['id']], '2026-10-08T00:00:00+00:00')
    main_window.current_page_id = 'default-canvas'
    main_window._open_successor_after_trash(None, ['default-canvas'])
    assert main_window.view.current_canvas_id is None
    assert not item.isVisible()
    assert item in list(main_window.view.scene.items_for_save())


def test_purge_survives_save_and_preserves_other_canvas(main_window, tmp_path):
    import sqlite3
    from prism.fileio.sql import SQLiteIO
    item, node, service = populate(main_window)
    scene = main_window.view.scene
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    kept = PrismPixmapItem(image)
    kept.canvas_id = 'new-canvas'
    scene.addItem(kept)
    path = str(tmp_path / 'purge.prism')
    writer = SQLiteIO(path, scene, create_new=True)
    writer.write()
    writer.connection.close()
    service.purge([node['id']])
    main_window._on_trash_purged(['default-canvas'])
    writer = SQLiteIO(path, scene)
    writer.write()
    writer.connection.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT count(*) FROM items').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM sqlar').fetchone()[0] == 1
    assert kept in list(scene.items_for_save())
    assert item not in list(scene.items_for_save())


def test_empty_export_does_not_include_hidden_items(view):
    from prism.fileio.export import SceneToPixmapExporter, SceneToSVGExporter
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    item.setVisible(False)
    assert view.scene.itemsBoundingRect(items=[]).isEmpty()
    assert SceneToPixmapExporter(view.scene).render_to_image().size() == QtCore.QSize(1, 1)
    assert not list(SceneToSVGExporter(view.scene).render_to_svg().iter('image'))


def test_inserting_text_after_last_canvas_trashed_creates_live_canvas(main_window):
    from prism.items import PrismTextItem
    item, node, service = populate(main_window)
    new_node = next(n for n in service.nodes if n.get('pageId') == 'new-canvas')
    service.trash([new_node['id']], '2026-10-08T00:00:00+00:00')
    main_window.open_workspace_page('canvas', main_window._first_canvas_id())
    main_window.view.on_action_insert_text()
    text = next(i for i in main_window.view.scene.items_for_save()
                if isinstance(i, PrismTextItem))
    assert text.canvas_id == main_window.view.current_canvas_id
    assert text.canvas_id not in ('default-canvas', 'new-canvas', None)
    assert text.isVisible()
    assert not item.isVisible()
    text.exit_edit_mode()


def test_clipboard_copies_belong_to_destination_canvas(main_window, qapp):
    from prism.actions.clipboard_workflow import paste
    item, node, service = populate(main_window)
    scene = main_window.view.scene
    scene.internal_clipboard = [item]
    mime = QtCore.QMimeData()
    mime.setData('prism/items', b'1')
    qapp.clipboard().setMimeData(mime)
    main_window.open_workspace_page('canvas', 'new-canvas')
    paste(main_window.view)
    copied = next(i for i in scene.items_for_save() if i is not item)
    assert copied.canvas_id == 'new-canvas'
    assert copied.isVisible()
