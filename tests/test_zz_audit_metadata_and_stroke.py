"""Guards for the 2026-10-23 audit data-loss fixes (A5).

A5: a finished stroke must land on the undo stack (Ctrl+Z removes it)
and mark the project dirty. It used to bypass the stack while the
eraser did not -- "drawing cannot be undone, erasing can".

(The video/GLB metadata half of the audit fix, A1, is covered by
tests/_verify_video_glb_metadata.py: constructing video items under
pytest-qt crashes natively on this machine.)
"""
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.items import DRAW_TOOL_PEN, PrismPathItem


@pytest.fixture
def view(qapp):
    from prism.view import PrismGraphicsView

    host = QtWidgets.QMainWindow()
    result = PrismGraphicsView(qapp, host)
    host.show()
    yield result
    host.close()


def stroke(view, points):
    press = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress,
                              QtCore.QPointF(*points[0]),
                              QtCore.Qt.MouseButton.LeftButton,
                              QtCore.Qt.MouseButton.LeftButton,
                              QtCore.Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(press)
    for x, y in points[1:-1]:
        move = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove,
                                 QtCore.QPointF(x, y),
                                 QtCore.Qt.MouseButton.NoButton,
                                 QtCore.Qt.MouseButton.LeftButton,
                                 QtCore.Qt.KeyboardModifier.NoModifier)
        view.mouseMoveEvent(move)
    release = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonRelease,
                                QtCore.QPointF(*points[-1]),
                                QtCore.Qt.MouseButton.LeftButton,
                                QtCore.Qt.MouseButton.NoButton,
                                QtCore.Qt.KeyboardModifier.NoModifier)
    view.mouseReleaseEvent(release)


def test_finished_stroke_is_undoable_and_marks_dirty(view):
    view.start_draw_mode()
    view.set_drawing_tool(DRAW_TOOL_PEN)
    stroke(view, [(50, 50), (60, 60), (70, 70)])

    paths = [item for item in view.scene.items()
             if isinstance(item, PrismPathItem)]
    assert len(paths) == 1, 'one finished stroke must exist'

    # A5 part 1: Ctrl+Z (undo) removes the stroke, like the eraser can.
    stack = view.scene.undo_stack
    assert not stack.isClean(), 'a finished stroke must dirty the undo stack'
    stack.undo()
    remaining = [item for item in view.scene.items()
                 if isinstance(item, PrismPathItem)]
    assert not remaining, 'undo must remove the finished stroke'

    # A5 part 2: redraw restores it.
    stack.redo()
    restored = [item for item in view.scene.items()
                if isinstance(item, PrismPathItem)]
    assert len(restored) == 1, 'redo must restore the stroke'

    # The view must know the project is dirty afterwards.
    assert view.is_dirty(), 'drawing must mark the project dirty'
