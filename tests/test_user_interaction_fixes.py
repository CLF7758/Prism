from PyQt6 import QtCore, QtGui, QtWidgets
from prism.items import PrismPathItem
from prism.widgets.resource_tree_model import ResourceTreeModel
from prism.workspace_service import ResourceTreeService
from tests.test_asset_browser import photo


def test_rename_edit_role_keeps_existing_title(qapp):
    service = ResourceTreeService('rename')
    folder = service.createFolder('canvas', 'Existing name')
    model = ResourceTreeModel(service)
    index = model.index_for_id(folder['id'])
    assert index.data(QtCore.Qt.ItemDataRole.EditRole) == 'Existing name'


def test_inspector_locates_and_selects_original(main_window):
    w = main_window
    page = w.workspace_service.add('canvas', 'Target')
    item = photo(w, page['id'], 'Image')
    w.asset_browser.open('all')
    panel = w.view._detail_panel
    panel._item = item
    panel._open_item_canvas()
    assert w.tabs.currentWidget() is w.canvas_page
    assert w.view.current_canvas_id == page['id']
    assert item.isSelected()
    assert not any(button.isChecked() for button in w.view.category_panel._nav_buttons.values())


def test_annotations_are_discarded_for_navigation_and_tags(main_window):
    w = main_window
    photo(w, 'default-canvas', 'Image', ['tag'])
    w.asset_browser.open('all')
    canvas = w.asset_browser.list
    for mode in ('unclassified', 'untagged', 'recent', 'all'):
        path = canvas.scene().addPath(QtGui.QPainterPath())
        canvas.scene().addText('Temporary')
        w.asset_browser.open(mode)
        assert canvas.scene().items() == [canvas._layer]
    canvas.scene().addText('Tag annotation')
    w.view.category_panel._browse_tag('tag')
    assert canvas.scene().items() == [canvas._layer]


def test_eraser_finds_stroke_below_selection_overlay(view):
    stroke = PrismPathItem([(0, 0), (100, 0)], width=8)
    view.scene.addItem(stroke)
    overlay = QtWidgets.QGraphicsRectItem(-20, -20, 150, 50)
    overlay.setZValue(100000)
    view.scene.addItem(overlay)
    view._erase_at(view.mapFromScene(QtCore.QPointF(50, 0)))
    assert stroke.scene() is None
    view.scene.undo_stack.undo()
    assert stroke.scene() is view.scene


def test_tags_are_directly_editable(main_window):
    panel = main_window.view._detail_panel
    assert not panel._tag_search.isHidden()
    assert panel._add_tag_button.isHidden()


def test_arrow_tip_has_no_protruding_round_line(view):
    from prism.items import ARROW_END
    item = PrismPathItem([(50, 50), (200, 50)], width=20, arrow=ARROW_END)
    view.scene.addItem(item)
    image = QtGui.QImage(250, 100, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QtGui.QPainter(image)
    item.paint(painter, QtWidgets.QStyleOptionGraphicsItem(), None)
    painter.end()
    assert image.pixelColor(207, 50).alpha() == 0
    assert image.pixelColor(190, 50).alpha() > 0


def test_embedded_preview_scales_in_worker_without_original_decode(monkeypatch):
    import base64
    from prism import image_decode
    image = QtGui.QImage(192, 96, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    value = [192, 96, base64.b64encode(bytes(data)).decode()]
    decoded, original = image_decode.decode_thumbnail(value, (1920, 960), (None, None, None), 32)
    assert decoded.size() == QtCore.QSize(32, 16)
    assert original == QtCore.QSize(1920, 960)


def test_hidden_canvas_does_not_schedule_preview_work(main_window):
    w = main_window
    item = photo(w, 'default-canvas', 'Image')
    renderer = w.view._overview_renderer
    renderer.frame = (QtGui.QImage(1, 1, QtGui.QImage.Format.Format_RGB32), QtCore.QRectF(0, 0, 100, 100))
    w.asset_browser.open('all')
    assert not w.view.isVisible()
    renderer._display_ready(item)
    assert not renderer.display_timer.isActive()
    renderer.queue_visible_details()
    assert getattr(item, '_display_decode_request', None) is None


def test_asset_layout_starts_in_viewport_after_resize(main_window, qapp):
    w = main_window
    photo(w, 'default-canvas', 'Image')
    w.asset_browser.open('all')
    canvas = w.asset_browser.list
    w.resize(1400, 900)
    qapp.processEvents()
    origin = canvas.mapFromScene(canvas.cards[0].sceneBoundingRect().topLeft())
    assert canvas.viewport().rect().contains(origin)


def test_public_canvas_arrow_has_filled_head(main_window):
    from PyQt6.QtTest import QTest
    w = main_window
    w.asset_browser.open('all')
    canvas = w.asset_browser.list
    canvas.activate('arrow')
    start, end = QtCore.QPoint(100, 100), QtCore.QPoint(250, 100)
    QTest.mousePress(canvas.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=start)
    event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(end),
                             QtCore.QPointF(end), QtCore.Qt.MouseButton.NoButton,
                             QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(canvas.viewport(), event)
    stroke = canvas._stroke
    QTest.mouseRelease(canvas.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=end)
    assert stroke.pen().style() == QtCore.Qt.PenStyle.NoPen
    assert stroke.brush().style() == QtCore.Qt.BrushStyle.SolidPattern
    assert stroke.path().contains(canvas.mapToScene(QtCore.QPoint(246, 100)))
