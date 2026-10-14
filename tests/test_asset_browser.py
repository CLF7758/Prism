from PyQt6 import QtCore, QtGui
from prism.items import PrismPixmapItem
from prism import commands
from prism.fileio.sql import SQLiteIO


def test_loading_defers_browser_layout_until_assets_are_complete(main_window, monkeypatch):
    from unittest.mock import Mock
    browser = main_window.asset_browser
    arranged = Mock(wraps=browser.list.arrange)
    monkeypatch.setattr(browser.list, 'arrange', arranged)
    main_window.view._project_opening = True
    first = photo(main_window, 'default-canvas', 'First')
    browser.refresh()
    second = photo(main_window, 'default-canvas', 'Second')
    browser.refresh()
    arranged.assert_not_called()
    main_window.view._project_opening = False
    browser.open('all')
    assert browser.model.items == [first, second]
    arranged.assert_called_once()


def test_dense_material_canvas_keeps_frame_until_visible_previews_are_ready(main_window, qtbot, monkeypatch):
    from prism import lod
    from unittest.mock import Mock
    browser = main_window.asset_browser
    first = photo(main_window, 'default-canvas', 'First')
    second = photo(main_window, 'default-canvas', 'Second')
    main_window.show()
    browser.open('all')
    browser.model.replace([first, second]*1000)
    canvas = browser.list
    canvas.setTransform(QtGui.QTransform.fromScale(.2, .2))
    canvas.centerOn(canvas._content_bounds.center())
    qtbot.wait(50)
    assert len(canvas.visible_cards(canvas.mapToScene(canvas.viewport().rect()).boundingRect())) >= 128
    rendered = Mock(wraps=canvas.scene().render)
    monkeypatch.setattr(canvas.scene(), 'render', rendered)
    canvas.viewport().grab()
    while canvas._tiles.pending:
        canvas._tiles._work()
    frames = {key: value[0].cacheKey() for key, value in canvas._tiles.tiles.items()}
    assert frames
    canvas.viewport().grab()
    assert all(canvas._tiles.tiles[key][0].cacheKey() == value for key, value in frames.items())
    ready = {first: True, second: False}
    monkeypatch.setattr(lod, 'base_preview_ready', lambda item, *args: ready[item])
    canvas.request_display_refresh()
    canvas._publish_display()
    assert not canvas._tiles.stale
    ready[second] = True
    canvas._publish_display()
    assert canvas._tiles.stale
    canvas.viewport().grab()
    while canvas._tiles.pending:
        canvas._tiles._work()
    assert rendered.call_count > 0


def photo(window, page, title, tags=()):
    image = QtGui.QImage(24, 24, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#336699'))
    item = PrismPixmapItem(image, filename=title + '.png')
    item._canvas_id = page
    item._tags = list(tags)
    item._title = title
    window.view.scene.addItem(item)
    return item


def test_browser_queries_whole_project_and_locates_original(main_window):
    w = main_window
    other = w.workspace_service.add('canvas', 'Other')['id']
    a = photo(w, 'default-canvas', 'First', ['reference'])
    b = photo(w, other, 'Second')
    b.setPos(123, 456)
    w.asset_browser.open('all')
    assert set(w.asset_browser.model.items) == {a, b}
    w.asset_browser.open('untagged')
    assert w.asset_browser.model.items == [b]
    w.asset_browser.locate(w.asset_browser.model.index(0))
    assert w.view.current_canvas_id == other
    assert b.pos() == QtCore.QPointF(123, 456)
    assert b._canvas_id == other
    assert b.isSelected()


def test_unclassified_uses_default_canvas_not_legacy_categories(main_window):
    w = main_window
    page = w.workspace_service.add('canvas', 'Classified')['id']
    default = photo(w, 'default-canvas', 'Unclassified')
    default._categories = ['legacy category']
    assigned = photo(w, page, 'Classified')
    w.asset_browser.open('unclassified')
    assert w.asset_browser.model.items == [default]
    catalog = w.view.category_panel.catalog
    assert catalog.count_snapshot()['unclassified'] == catalog.unclassified_count() == 1
    assert not catalog.is_unclassified(assigned)
    w.search_controller.scope = ('navigation', 'unclassified')
    assert not w.search_controller.in_scope({'kind': 'asset', 'item': assigned})


def test_tag_tree_opens_compact_material_canvas(main_window):
    w = main_window
    tagged = photo(w, 'default-canvas', 'A', ['reference'])
    photo(w, 'default-canvas', 'B')
    w.view.category_panel.resource_tree.tag_requested.emit('reference')
    assert w.tabs.currentWidget() is w.asset_browser
    assert w.asset_browser.model.items == [tagged]
    assert not w.search_controller.active
    from tests.test_search_controller import submit
    submit(w, 'B')
    assert w.search_controller.active
    w.view.category_panel.resource_tree.tag_requested.emit('reference')
    assert w.tabs.currentWidget() is w.asset_browser
    assert w.asset_browser.model.items == [tagged]
    assert not w.search_controller.active


def test_folder_after_search_opens_same_material_canvas(main_window):
    w = main_window
    panel = w.view.category_panel
    folder = panel.resource_service.createFolder('canvas', 'Folder')
    panel.resource_service.createPage('canvas', 'Board', 'folder-board', folder['id'])
    item = photo(w, 'folder-board', 'Folder image')
    photo(w, 'default-canvas', 'Outside')
    from tests.test_search_controller import submit
    submit(w, 'Folder image')
    assert w.search_controller.active
    panel._browse_folder(panel.resource_tree.model().index_for_id(folder['id']))
    assert w.tabs.currentWidget() is w.asset_browser
    assert w.asset_browser.model.items == [item]
    assert not w.search_controller.active


def test_browser_search_tag_and_trash(main_window):
    w = main_window
    a = photo(w, 'default-canvas', 'First', ['reference'])
    b = photo(w, 'default-canvas', 'Second')
    w.asset_browser.open('all')
    w.asset_browser.search.setText('First')
    w.asset_browser.refresh()
    assert w.asset_browser.model.items == [a]
    w.asset_browser.search.clear()
    w.asset_browser.set_tag('reference', True)
    assert w.asset_browser.model.items == [a]
    w.asset_browser.set_tag('reference', False)
    index = w.asset_browser.model.index(w.asset_browser.model.items.index(a))
    w.asset_browser.list.selectionModel().select(index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    w.view.on_action_delete_items()
    w.asset_browser.refresh()
    assert w.asset_browser.model.items == [b]
    assert w.view.category_panel.catalog.trash_asset_count() == 1
    w.view.undo_stack.undo()
    w.asset_browser.refresh()
    assert set(w.asset_browser.model.items) == {a, b}


def test_history_restores_smart_folder_scope_and_search(main_window):
    browser = main_window.asset_browser
    photo(main_window, 'default-canvas', 'First')
    photo(main_window, 'default-canvas', 'Second')
    browser.saved_rule = {'title': 'Saved', 'text': 'First'}
    browser.search.setText('First')
    browser.open('all', title='Saved')
    browser.saved_rule = None
    browser.search.clear()
    browser.open('all')
    assert browser.model.rowCount() == 2
    browser.travel(-1)
    assert browser.saved_rule['text'] == 'First'
    assert browser.search.text() == 'First'
    assert browser.model.rowCount() == 1


def test_smart_folder_rules_persist(main_window, tmp_path):
    scene = main_window.view.scene
    rule = {'title': 'Reference', 'text': 'First', 'tag_expression': '', 'rating': 2}
    scene.library_queries = [rule]
    path = str(tmp_path / 'smart.prism')
    io = SQLiteIO(path, scene, create_new=True)
    io.write()
    scene.library_queries = []
    io.read_board_extras()
    assert scene.library_queries == [rule]
    io.connection.close()


def test_header_preserves_original_color_inspector(main_window):
    main_window.show()
    main_window.asset_browser.open('all')
    assert main_window.menuBar().isHidden()
    assert main_window._eagle_header_stack.currentWidget() is main_window.canvas_toolbar
    assert hasattr(main_window.view._detail_panel, '_cd_container')
    assert not hasattr(main_window.view.category_panel, 'tag_management_button')


def test_damaged_asset_is_counted_trashed_and_preserved_on_save_as(main_window, tmp_path):
    import json
    from prism.items import PrismErrorItem
    w = main_window
    original = str(tmp_path / 'damaged.prism')
    io = SQLiteIO(original, w.view.scene, create_new=True)
    io.create_schema_on_new()
    io.ex('INSERT INTO items(type,data) VALUES (?,?)', ('pixmap', json.dumps({'filename':'broken.png','canvasId':'default-canvas'})))
    io.ex('INSERT INTO sqlar(name,item_id,data) VALUES (?,?,?)', ('broken.png',1,b'not an image'))
    io.connection.commit()
    io.read()
    w.view.scene.add_queued_items()
    item = next(i for i in w.view.scene.items() if isinstance(i, PrismErrorItem))
    assert w.view.category_panel.catalog.total_count() == 1
    w.asset_browser.open('all')
    assert w.asset_browser.model.items == [item]
    assert w.asset_browser.storage_bytes([item]) == len(b'not an image')
    item.setSelected(True)
    w.view.on_action_delete_items()
    assert w.view.category_panel.catalog.total_count() == 0
    assert w.view.category_panel.catalog.trash_asset_count() == 1
    destination = SQLiteIO(str(tmp_path / 'copy.prism'), w.view.scene, create_new=True)
    destination.write()
    assert destination.fetchone('SELECT data FROM sqlar')[0] == b'not an image'
    metadata = json.loads(destination.fetchone('SELECT data FROM items')[0])
    assert metadata['deletedAt']
    assert metadata['filename'] == 'broken.png'
    assert '_asset_source' not in metadata
    destination.connection.close()
    destination = SQLiteIO(str(tmp_path / 'copy.prism'), w.view.scene)
    destination.write()
    assert destination.fetchone('SELECT count(*) FROM items')[0] == 1
    io.connection.close()
    destination.connection.close()
