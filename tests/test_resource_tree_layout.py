"""Keep the original compact native tree, without per-section stretch areas."""
from PyQt6 import QtCore, QtGui
from unittest.mock import patch
from pathlib import Path
import pytest
from prism.widgets.resource_tree_model import UI_SECTIONS
from PyQt6 import QtWidgets, QtTest


def test_collapsed_categories_are_adjacent_at_top(main_window, qtbot):
    window = main_window
    window.resize(1200, 1000)
    window.show()
    panel = window.view.category_panel
    tree = panel.resource_tree
    assert not hasattr(panel, 'resource_sections')
    assert not tree.rootIndex().isValid()
    tree.collapseAll()
    qtbot.wait(20)
    roots = [tree.model()._index_for(section) for section in UI_SECTIONS]
    rects = [tree.visualRect(index) for index in roots]
    assert len(rects) == 6
    assert rects[0].top() <= 2
    for before, after in zip(rects, rects[1:]):
        assert 0 <= after.top() - before.bottom() <= 2
    assert rects[-1].bottom() < 250


def test_open_canvas_uses_only_actual_row_height(main_window, qtbot):
    window = main_window
    window.resize(1200, 1000)
    window.show()
    tree = window.view.category_panel.resource_tree
    model = tree.model()
    tree.collapseAll()
    canvas = model._index_for('canvas')
    mindmap = model._index_for('mindmap')
    tree.expand(canvas)
    qtbot.wait(20)
    children = [model.index(i, 0, canvas) for i in range(model.rowCount(canvas))
                if not tree.isRowHidden(i, canvas)]
    assert children
    last = tree.visualRect(children[-1])
    next_category = tree.visualRect(mindmap)
    assert 0 <= next_category.top() - last.bottom() <= 2
    assert next_category.top() < 180
    # All category indexes remain peers; expanding one never reparents others.
    assert all(not model._index_for(section).parent().isValid() for section in UI_SECTIONS)


def test_every_category_keeps_disclosure_arrow_even_empty(main_window, qtbot):
    main_window.resize(1200, 800)
    main_window.show()
    tree = main_window.view.category_panel.resource_tree
    tree.collapseAll()
    qtbot.wait(10)
    for section in UI_SECTIONS:
        root = tree.model()._index_for(section)
        assert tree.model().hasChildren(root)
        rect = tree.visualRect(root)
        arrow = QtCore.QPoint(rect.left() - tree.indentation() // 2, rect.center().y())
        QtTest.QTest.mouseClick(tree.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=arrow)
        # Qt keeps a genuinely empty root closed; populated roots toggle.
        has_rows = any(not tree.isRowHidden(i, root) for i in range(tree.model().rowCount(root)))
        if has_rows:
            assert tree.isExpanded(root), section
        QtTest.QTest.mouseClick(tree.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=arrow)
        assert not tree.isExpanded(root), section


def test_scrolled_category_sticks_at_top_without_changing_tree_layout(qtbot):
    from prism.workspace_service import ResourceTreeService
    from prism.widgets.resource_tree_model import ResourceTreeModel, SECTION_ROLE
    from prism.widgets.resource_tree_view import ResourceTreeView
    service = ResourceTreeService('sticky-category', model_section_enabled=True)
    model = ResourceTreeModel(service)
    from types import SimpleNamespace
    model.set_tags([SimpleNamespace(name='Tag '+str(i), label='Tag '+str(i),
                                   parent=None, aliases=[], count=0) for i in range(60)])
    for section in UI_SECTIONS:
        if section != 'tag':
            for i in range(60):
                service.createFolder(section, f'{section} {i}')
    tree = ResourceTreeView()
    tree.setModel(model)
    qtbot.addWidget(tree)
    tree.resize(350, 320)
    tree.show()
    for section in UI_SECTIONS:
        root = model._index_for(section)
        tree.expand(root)
        target = model.index(25, 0, root)
        tree.scrollTo(target, QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
        qtbot.wait(10)
        sticky = tree._sticky_section
        assert not sticky.isHidden(), section
        assert sticky.index.data(SECTION_ROLE) == section
        assert sticky.y() == 0
        before = tree.visualRect(target).y()
        tree.verticalScrollBar().setValue(tree.verticalScrollBar().value() + 2)
        # Check immediately: viewport scrolling must not move its child header
        # before a later paint happens to move it back.
        assert sticky.y() == 0
        qtbot.wait(10)
        assert sticky.y() == 0
        assert tree.visualRect(target).y() < before
        assert tree.indexAt(QtCore.QPoint(40, 5)) == root
        assert not tree.rootIndex().isValid()
    # The stuck arrow collapses only that root; all six stay peer rows.
    QtTest.QTest.mouseClick(tree._sticky_section, QtCore.Qt.MouseButton.LeftButton,
                           pos=QtCore.QPoint(5, 10))
    assert not tree.isExpanded(model._index_for('tag'))
    tree.collapseAll()
    tree.verticalScrollBar().setValue(0)
    qtbot.wait(10)
    assert tree._sticky_section.isHidden()
    roots = [tree.visualRect(model._index_for(section)) for section in UI_SECTIONS]
    assert roots[-1].bottom() < 250


@pytest.mark.parametrize('shipped_theme', [False, True])
def test_new_project_really_paints_all_six_arrows(main_window, qtbot, qapp, request, shipped_theme):
    if shipped_theme:
        previous = qapp.styleSheet()
        request.addfinalizer(lambda: qapp.setStyleSheet(previous))
        root = Path(__file__).resolve().parents[1]
        qss = (root / 'prism/assets/theme.qss').read_text(encoding='utf-8')
        qapp.setStyleSheet(qss.replace('url(assets/icons/', 'url(' +
                                      (root / 'prism/assets/icons').as_posix() + '/'))
    main_window.resize(1200, 800)
    main_window.show()
    tree = main_window.view.category_panel.resource_tree
    tree.collapseAll()
    qtbot.wait(10)

    def render():
        image = QtGui.QImage(tree.viewport().size(), QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        tree.viewport().render(image)
        return image

    actual = render()
    with patch('prism.widgets.resource_tree_view.icon', return_value=QtGui.QIcon()):
        without_icons = render()
    for section in UI_SECTIONS:
        rect = tree.visualRect(tree.model()._index_for(section))
        branch = QtCore.QRect(rect.left() - tree.indentation(), rect.top(),
                              tree.indentation(), rect.height())
        # Prove pixels exist, not just model.hasChildren or an icon option.
        assert actual.copy(branch) != without_icons.copy(branch), section


def test_sticky_header_is_fully_opaque_with_transparent_view_palette(qtbot):
    from prism.workspace_service import ResourceTreeService
    from prism.widgets.resource_tree_model import ResourceTreeModel
    from prism.widgets.resource_tree_view import ResourceTreeView
    service = ResourceTreeService('opaque-header')
    for i in range(80):
        service.createFolder('canvas', 'Canvas '+str(i))
    model = ResourceTreeModel(service)
    tree = ResourceTreeView()
    tree.setModel(model)
    qtbot.addWidget(tree)
    tree.resize(350, 300)
    tree.show()
    root = model._index_for('canvas')
    tree.expand(root)
    tree.scrollTo(model.index(30, 0, root), QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
    qtbot.wait(10)
    palette = tree.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(0, 0, 0, 0))
    tree.setPalette(palette)
    header = tree._sticky_section
    image = QtGui.QImage(header.size(), QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    header.render(image)
    assert all(image.pixelColor(x, y).alpha() == 255
               for y in range(image.height()) for x in range(image.width()))
