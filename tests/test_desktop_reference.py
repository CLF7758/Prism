from PyQt6 import QtCore, QtGui, QtWidgets
from prism.widgets.desktop_reference import DesktopReference
from prism.widgets.canvas_toolbar import CanvasToolbar


def test_reference_reuses_native_tools_and_renders_annotation(qapp, qtbot):
    image = QtGui.QImage(240, 160, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    window = DesktopReference(image)
    window.show()
    assert isinstance(window.toolbar, CanvasToolbar)
    assert window.canvas.reference.pixmap().toImage().pixelColor(0, 0).blue() == 255
    window.show_tools()
    assert window.canvas.tool == 'pen'
    path = QtGui.QPainterPath(QtCore.QPointF(30, 30))
    path.lineTo(150, 100)
    stroke = window.canvas.scene().addPath(path, QtGui.QPen(QtGui.QColor('red'), 12))
    result = window.rendered_image()
    assert result.pixelColor(90, 65).red() > 200
    window.canvas.erase_annotation(window.canvas.mapFromScene(QtCore.QPointF(90, 65)))
    assert stroke.scene() is None
    assert window.rendered_image().pixelColor(90, 65).blue() == 255
    window.set_arrow('end')
    assert window.canvas.tool == 'arrow'
    window.show_opacity()
    window.opacity_slider.setValue(45)
    assert abs(window.windowOpacity()-.45) < .01
    assert '45%' in window.opacity_label.text()
    assert window.rendered_image().pixelColor(90, 65).alpha() == 255
    window.close()


def test_visual_search_controls_do_not_overlap(main_window, qtbot):
    search = main_window.search_controller
    main_window.resize(1200, 800)
    main_window.show()
    search.scope_combo.show()
    search.filter_button.show()
    search.info.show()
    search.info.setText('正在本地检索 100/2742')
    qtbot.wait(30)
    input_rect = QtCore.QRect(search.input.mapTo(search.header, QtCore.QPoint()), search.input.size())
    for widget in (search.scope_combo, search.filter_button, search.info):
        rect = QtCore.QRect(widget.mapTo(search.header, QtCore.QPoint()), widget.size())
        assert not rect.intersects(input_rect)


def test_context_removes_requested_entries_and_capture_shortcut(view):
    from prism.actions.actions import actions
    from prism.actions.menu_structure import menu_structure
    view._inject_canvas_tools()
    assert not any(a.text() == '钉在桌面' for a in view.context_menu.actions())
    assert 'Ctrl+Alt+A' in actions['screen_capture'].shortcuts
    assert not any('capture_inbox' in menu['items'] for menu in menu_structure if isinstance(menu.get('items'), list))


def test_reference_draw_uses_same_path_item_and_arrows(qapp, qtbot):
    from prism.items import PrismPathItem
    image = QtGui.QImage(320, 240, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#25354b'))
    window = DesktopReference(image)
    window.show()
    window.set_arrow('both')
    viewport = window.canvas.viewport()
    qtbot.mousePress(viewport, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(30, 30))
    qtbot.mouseMove(viewport, QtCore.QPoint(180, 130))
    qtbot.mouseRelease(viewport, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(180, 130))
    path = next(item for item in window.canvas.scene().items() if isinstance(item, PrismPathItem))
    assert path.arrow() == 'both' and len(path.points()) == 2
    window.close()


def test_hover_leave_after_drawing_does_not_require_main_scene(main_window, qtbot):
    from prism.items import PrismPathItem
    from prism.actions.drawing_workflow import CanvasDrawingMixin
    image = QtGui.QImage(400, 250, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('black'))
    window = DesktopReference(image, main_window)
    window.show()
    qtbot.wait(10)
    assert window.toolbar.height() == 36
    for tool in ('pen', 'eraser', 'line', 'arrow', 'text'):
        assert window.toolbar.tools[tool].size() == main_window.canvas_toolbar.tools[tool].size()
    window.canvas.resetTransform()
    window.canvas.scale(.25, .25)
    window.set_width(8.)
    assert window.view._scene_pen_width() == 32.
    assert window.view._scene_pen_width() == CanvasDrawingMixin._scene_pen_width(window.view)
    item = PrismPathItem([(10, 10), (100, 100)])
    window.canvas.scene().addItem(item)
    item.set_cursor(QtGui.QCursor(QtCore.Qt.CursorShape.CrossCursor))
    item.hoverLeaveEvent(None)
    assert window.canvas.viewport().cursor().shape() != QtCore.Qt.CursorShape.CrossCursor
    # A plain Qt scene is also valid for a non-editor preview of this item.
    window.canvas.scene().removeItem(item)
    plain = QtWidgets.QGraphicsScene()
    plain.addItem(item)
    item.hoverLeaveEvent(None)
    plain.clear()
    window.close()


def test_tools_follow_owner_and_hide_on_other_reference(qapp, qtbot):
    image = QtGui.QImage(320, 240, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    a, b = DesktopReference(image), DesktopReference(image)
    a.show()
    a.show_tools()
    qtbot.wait(5)
    assert a.toolbar.width() < 500
    a.move(100, 100)
    a._position_tools()
    old = a.toolbar.pos()
    a.move(140, 120)
    assert a.toolbar.pos()-old == QtCore.QPoint(40, 20)
    b.show()
    b.show_tools()
    assert a.toolbar.isHidden()
    assert a.canvas.tool == 'select'
    b._application_state(QtCore.Qt.ApplicationState.ApplicationInactive)
    assert b.toolbar.isHidden()
    assert b.canvas.tool == 'select'
    a.close()
    b.close()


def test_nested_menu_wait_is_not_counted_as_a_blocking_dispatch():
    from collections import Counter
    from unittest.mock import Mock
    from prism.performance_trace import PerformanceTrace
    from types import SimpleNamespace
    trace = SimpleNamespace()
    trace.counts, trace.max_dispatch, trace.last_action = Counter(), 0, None
    trace.record = Mock()
    PerformanceTrace.dispatch_finished(trace, object(), 82, 6500, nested_loop=True)
    assert trace.record.call_args.args[0] == 'nested_dispatch'
    assert trace.max_dispatch == 0
    PerformanceTrace.dispatch_finished(trace, object(), 82, 800)
    assert trace.record.call_args.args[0] == 'slow_dispatch'
    assert trace.max_dispatch == 800


def test_reference_uses_full_scene_selection_contract(qapp, qtbot):
    from prism.scene import PrismGraphicsScene
    from prism.items import PrismPathItem
    image = QtGui.QImage(400, 260, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#253040'))
    window = DesktopReference(image)
    window.show()
    scene = window.canvas.scene()
    assert isinstance(scene, PrismGraphicsScene)
    a = PrismPathItem([(10, 10), (100, 100)])
    b = PrismPathItem([(110, 10), (200, 100)])
    scene.addItem(a)
    scene.addItem(b)
    scene.active_mode = scene.MOVE_MODE
    for _ in range(10):
        a.setSelected(True)
        assert scene.has_selection() and scene.has_single_selection()
        a.hoverLeaveEvent(None)
        b.setSelected(True)
        assert scene.has_multi_selection()
        assert scene.multi_select_item.scene() is scene
        image = window.rendered_image()
        assert not image.isNull()
        scene.clearSelection()
        assert not scene.has_selection()
        qtbot.wait(1)
    scene.removeItem(a)
    scene.removeItem(b)
    window.close()
