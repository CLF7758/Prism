from PyQt6 import QtCore, QtGui
from prism.items import PrismPixmapItem
from prism import commands
from prism.fileio.sql import SQLiteIO


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
    assert main_window._eagle_header_stack.currentWidget() is main_window.asset_browser.toolbar
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
