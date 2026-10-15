"""Desktop reference shell using Prism's existing canvas drawing controls."""
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.widgets.asset_canvas import AssetCanvas
from prism.widgets.canvas_toolbar import CanvasToolbar
from prism.actions.drawing_workflow import CanvasDrawingMixin
from prism.scene import PrismGraphicsScene
from prism.ui_tokens import COLOURS_DARK


class ReferenceTools(QtCore.QObject):
    """State adapter only; the actual controls and pixel mapping are shared."""
    drawing_state_changed = QtCore.pyqtSignal()
    DRAW_MODE = 'draw'
    active_mode = None
    _drawing_tool = 'pen'
    _text_tool_armed = False
    _arrow = 'none'

    def __init__(self, host, original):
        super().__init__(host)
        self.host = host
        self._pen_color = getattr(original, '_pen_color', '#ff5252')
        self._pen_width = getattr(original, '_pen_width', 4.)
        self._line_style = getattr(original, '_line_style', 'solid')

    def transform(self):
        return self.host.canvas.transform()

    _scene_pen_width = CanvasDrawingMixin._scene_pen_width

    def set_pen_width(self, value):
        self._pen_width = max(.5, min(50., value))
        self.drawing_state_changed.emit()

    def set_line_style(self, value):
        self._line_style = value
        self.drawing_state_changed.emit()

    def pick_pen_colour(self):
        self.host.choose_colour()

    def window(self):
        return self.host

    def on_action_screen_capture(self):
        pass  # Not present in the reference annotation toolbar.

    def on_action_pin_reference(self):
        pass


class ReferenceScene(PrismGraphicsScene):
    """Use the complete editor scene contract, with reference-local gestures."""
    def __init__(self, parent, undo_stack):
        super().__init__(undo_stack)
        self.setParent(parent)

    def mousePressEvent(self, event):
        QtWidgets.QGraphicsScene.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):
        QtWidgets.QGraphicsScene.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event):
        QtWidgets.QGraphicsScene.mouseReleaseEvent(self, event)

    def mouseDoubleClickEvent(self, event):
        QtWidgets.QGraphicsScene.mouseDoubleClickEvent(self, event)


class ReferenceCanvas(AssetCanvas):
    """Reuse material-canvas strokes and erasing, with window dragging in select."""
    def __init__(self, host, image):
        super().__init__(host)
        old_scene = self.scene()
        self.setScene(ReferenceScene(self, self.undo_stack))
        old_scene.removeItem(self._layer)
        self.scene().addItem(self._layer)
        self.scene().changed.connect(self._invalidate_display_frame)
        old_scene.deleteLater()
        self.scene().cursor_changed.connect(self.viewport().setCursor)
        self.scene().cursor_cleared.connect(self.viewport().unsetCursor)
        self.reference = self.scene().addPixmap(QtGui.QPixmap.fromImage(image))
        self.reference.setZValue(-1)
        self.scene().setSceneRect(self.reference.boundingRect())
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.window_drag = None

    def get_scale(self):
        return abs(self.transform().m11())

    def reset_previous_transform(self, toggle_item=None):
        self.previous_transform = None

    def _history_changed(self, _enabled):
        # The reference owns this history; never redirect to the main canvas.
        pass

    def mousePressEvent(self, event):
        if self.tool == 'select' and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.window_drag = event.globalPosition().toPoint()-self.window().pos()
            event.accept()
        elif self.tool in ('pen', 'line', 'arrow') and event.button() == QtCore.Qt.MouseButton.LeftButton:
            from prism.items import PrismPathItem
            point = self.mapToScene(event.position().toPoint())
            settings = self.parent().view
            self._stroke = PrismPathItem(points=[(point.x(), point.y())],
                color=settings._pen_color, width=settings._scene_pen_width(),
                tool='pen' if self.tool == 'pen' else 'line',
                arrow=self.window().arrow if self.tool == 'arrow' else 'none',
                line_style=settings._line_style)
            self._stroke.setZValue(1)
            self.scene().addItem(self._stroke)
            event.accept()
        elif self.tool != 'select' and event.button() == QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
        else:
            QtWidgets.QGraphicsView.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):
        if self.window_drag is not None:
            self.window().move(event.globalPosition().toPoint()-self.window_drag)
            event.accept()
        elif self._stroke is not None:
            point = self.mapToScene(event.position().toPoint())
            points = self._stroke.points()
            self._stroke.set_points((points if self.tool == 'pen' else points[:1])+[(point.x(), point.y())])
            event.accept()
        elif self.tool == 'eraser':
            super().mouseMoveEvent(event)
        else:
            QtWidgets.QGraphicsView.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event):
        if self.window_drag is not None:
            self.window_drag = None
            event.accept()
        elif self._stroke is not None:
            super().mouseReleaseEvent(event)
        else:
            QtWidgets.QGraphicsView.mouseReleaseEvent(self, event)

    def wheelEvent(self, event):
        host = self.window()
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            host.setWindowOpacity(max(.15, min(1, host.windowOpacity()+(.05 if event.angleDelta().y()>0 else -.05))))
        else:
            factor = 1.12 if event.angleDelta().y()>0 else 1/1.12
            host.resize(max(96, min(2400, round(host.width()*factor))), max(72, min(1800, round(host.height()*factor))))
        event.accept()

    def resizeEvent(self, event):
        QtWidgets.QGraphicsView.resizeEvent(self, event)
        if hasattr(self, 'reference'):
            self.fitInView(self.reference.boundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.window().set_tool('select')
            self.window().toolbar.hide()
        else:
            QtWidgets.QGraphicsView.keyPressEvent(self, event)

    def activate(self, tool):
        super().activate(tool)
        if tool == 'arrow':
            self.window().arrow = 'end'


class DesktopReference(QtWidgets.QWidget):
    send_to_canvas = QtCore.pyqtSignal(object)
    def __init__(self, image, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.view = ReferenceTools(self, getattr(parent, 'view', None))
        self.arrow = 'none'
        self.opacity_dialog = None
        self.setWindowTitle('Prism 桌面参考图')
        self.setStyleSheet('QMenu{background:%(panel-bg)s;color:%(text-primary)s;border:1px solid %(border-subtle)s;padding:5px;} QMenu::item{padding:7px 22px;} QMenu::item:selected{background:%(selected-bg)s;}' % COLOURS_DARK)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = ReferenceCanvas(self, image)
        layout.addWidget(self.canvas)
        self.toolbar = CanvasToolbar(self.view, None, self, canvas=self.canvas, annotation_only=True)
        self.toolbar.setWindowFlags(QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.toolbar.hide()
        self._visibility_check = QtCore.QTimer(self)
        self._visibility_check.setSingleShot(True)
        self._visibility_check.setInterval(100)
        self._visibility_check.timeout.connect(self._check_tool_focus)
        QtWidgets.QApplication.instance().applicationStateChanged.connect(self._application_state)
        self.installEventFilter(self)
        self.toolbar.installEventFilter(self)
        screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
        size = image.size().scaled(screen.availableGeometry().size()*.7, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.resize(max(96, size.width()), max(72, size.height()))
        self.move(screen.availableGeometry().center()-self.rect().center())

    def set_tool(self, tool):
        self.canvas.activate('arrow' if tool == 'line' and self.arrow != 'none' else tool)
        self.toolbar.refresh()

    def set_width(self, value):
        self.view.set_pen_width(value)

    def set_arrow(self, value):
        self.set_tool('line')
        self.arrow = value
        self.canvas.tool = 'line' if value == 'none' else 'arrow'
        self.toolbar.refresh()

    def choose_colour(self):
        colour = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.view._pen_color), self, '画笔颜色')
        if colour.isValid():
            self.view._pen_color = colour.name()
            self.view.drawing_state_changed.emit()

    def show_tools(self):
        for window in QtWidgets.QApplication.topLevelWidgets():
            if isinstance(window, DesktopReference) and window is not self:
                window.hide_tools()
        self._position_tools()
        self.toolbar.show()
        self.set_tool('pen')

    def _position_tools(self):
        screen = self.screen().availableGeometry()
        self.toolbar.move(max(screen.left(), min(self.x(), screen.right()-self.toolbar.width())), max(screen.top(), min(self.y()+self.height()+8, screen.bottom()-self.toolbar.height())))

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, 'toolbar') and self.toolbar.isVisible():
            self._position_tools()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'toolbar') and self.toolbar.isVisible():
            self._position_tools()

    def hide_tools(self):
        self.toolbar.hide()
        self.set_tool('select')

    def _application_state(self, state):
        if state != QtCore.Qt.ApplicationState.ApplicationActive:
            self.hide_tools()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.WindowDeactivate:
            self._visibility_check.start()
        return False

    def _check_tool_focus(self):
        app = QtWidgets.QApplication.instance()
        popup = app.activePopupWidget()
        if popup is not None and self.toolbar.isAncestorOf(popup):
            return
        window = app.activeWindow()
        if window is self or window is self.toolbar or (window is not None and (self.toolbar.isAncestorOf(window) or self.isAncestorOf(window))):
            return
        self.hide_tools()

    def rendered_image(self):
        rect = self.canvas.reference.boundingRect()
        image = QtGui.QImage(rect.size().toSize(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(image)
        self.canvas.scene().render(painter, QtCore.QRectF(image.rect()), rect)
        painter.end()
        return image

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self)
        menu.addAction('显示标注工具', self.show_tools)
        menu.addAction('选择 / 移动窗口', lambda: self.set_tool('select'))
        menu.addAction('复制图片', lambda: QtWidgets.QApplication.clipboard().setImage(self.rendered_image()))
        menu.addAction('另存为…', self.save_image)
        menu.addAction('图片透明度…', self.show_opacity)
        menu.addAction('送入 Prism 当前画布', lambda: self.send_to_canvas.emit(self.rendered_image()))
        menu.addSeparator()
        menu.addAction('关闭参考图', self.close)
        menu.exec(event.globalPos())

    def show_opacity(self):
        if self.opacity_dialog is None:
            dialog = self.opacity_dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle('参考图透明度')
            dialog.setWindowFlags(dialog.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
            dialog.setStyleSheet('QDialog{background:%(panel-bg)s;color:%(text-primary)s;} QLabel{color:%(text-primary)s;}' % COLOURS_DARK)
            layout = QtWidgets.QVBoxLayout(dialog)
            self.opacity_label = QtWidgets.QLabel()
            layout.addWidget(self.opacity_label)
            self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.opacity_slider.setRange(15, 100)
            self.opacity_slider.setMinimumWidth(240)
            self.opacity_slider.valueChanged.connect(self.set_reference_opacity)
            layout.addWidget(self.opacity_slider)
            reset = QtWidgets.QPushButton('恢复不透明')
            reset.clicked.connect(lambda: self.opacity_slider.setValue(100))
            layout.addWidget(reset)
        self.opacity_slider.setValue(round(self.windowOpacity()*100))
        self.set_reference_opacity(self.opacity_slider.value())
        self.opacity_dialog.show()
        self.opacity_dialog.raise_()

    def set_reference_opacity(self, percent):
        percent = max(15, min(100, percent))
        self.setWindowOpacity(percent/100)
        if self.opacity_dialog is not None:
            self.opacity_label.setText(f'图片不透明度：{percent}%（Ctrl＋滚轮也可调节）')

    def save_image(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '保存参考图', '', 'PNG 图片 (*.png)')
        if path:
            self.rendered_image().save(path, 'PNG')

    def closeEvent(self, event):
        self._visibility_check.stop()
        QtWidgets.QApplication.instance().applicationStateChanged.disconnect(self._application_state)
        self.toolbar.close()
        event.accept()
