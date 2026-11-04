from types import SimpleNamespace
from PyQt6 import QtCore
from prism.widgets.asset_grid import AssetGrid


def wheel():
    accepted=[]
    return SimpleNamespace(modifiers=lambda:QtCore.Qt.KeyboardModifier.ControlModifier,
        angleDelta=lambda:QtCore.QPoint(0,120),pixelDelta=lambda:QtCore.QPoint(),
        position=lambda:QtCore.QPointF(100,100),accept=lambda:accepted.append(True)),accepted


def test_ctrl_wheel_grid_emits_size_and_respects_limits(qtbot):
    grid=AssetGrid();qtbot.addWidget(grid)
    values=[];grid.card_size_changed.connect(values.append)
    event,accepted=wheel()
    old=grid.thumbnailSize().width()
    grid.wheelEvent(event)
    assert values==[old+12] and accepted
    for _ in range(100):grid.wheelEvent(event)
    assert grid.thumbnailSize().width()==280


def test_canvas_slider_and_ctrl_wheel_share_real_zoom(main_window):
    view=main_window.view
    from PyQt6 import QtGui
    from prism.items import PrismPixmapItem
    image=QtGui.QImage(100,100,QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    view.scene.addItem(PrismPixmapItem(image))
    bar=main_window.canvas_toolbar
    bar.zoom_slider.setValue(100)
    assert abs(view.transform().m11()-2)<.001
    event,accepted=wheel()
    before=view.transform().m11()
    view.wheelEvent(event)
    assert view.transform().m11()>before and accepted
    assert bar.zoom_slider.value()>100


def test_trash_slider_changes_grid(qtbot,view):
    from prism.widgets.trash_panel import TrashPanel
    panel=TrashPanel(view);qtbot.addWidget(panel)
    panel.zoom_slider.setValue(200)
    assert panel.asset_list.thumbnailSize().width()==200
    event,_=wheel();panel.asset_list.wheelEvent(event)
    assert panel.zoom_slider.value()==212


def test_resource_tree_shift_selects_range_without_navigating(qtbot):
    from PyQt6 import QtTest
    from prism.workspace_service import ResourceTreeService
    from prism.widgets.resource_tree_model import ResourceTreeModel, NODE_ID_ROLE
    from prism.widgets.resource_tree_view import ResourceTreeView
    service=ResourceTreeService('range-project')
    ids=[service.createFolder('material',name) for name in ('a','b','c')]
    # Service returns nodes; normalize only stable ids for expectations.
    ids=[value['id'] if isinstance(value,dict) else value for value in ids]
    model=ResourceTreeModel(service);tree=ResourceTreeView();qtbot.addWidget(tree)
    tree.setModel(model);tree.resize(400,500);tree.show();tree.expandAll()
    qtbot.wait(10)
    opened=[];tree.open_material_requested.connect(opened.append)
    first=model._index_for(ids[0]);last=model._index_for(ids[-1])
    QtTest.QTest.mouseClick(tree.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=tree.visualRect(first).center())
    before=len(opened)
    QtTest.QTest.mouseClick(tree.viewport(),QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.ShiftModifier,pos=tree.visualRect(last).center())
    selected={index.data(NODE_ID_ROLE) for index in tree.selectedIndexes()}
    assert set(ids)<=selected
    assert len(opened)==before


def test_main_canvas_shift_click_adds_to_selection(main_window,qtbot):
    from PyQt6 import QtGui,QtTest
    from prism.items import PrismPixmapItem
    window=main_window;window.show();view=window.view
    image=QtGui.QImage(50,50,QtGui.QImage.Format.Format_RGB32);image.fill(QtGui.QColor('red'))
    left=PrismPixmapItem(image);right=PrismPixmapItem(image)
    right.setPos(150,0)
    view.scene.addItem(left);view.scene.addItem(right)
    view.fitInView(view.scene.itemsBoundingRect(),QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    left.setSelected(True)
    position=view.mapFromScene(right.sceneBoundingRect().center())
    QtTest.QTest.mouseClick(view.viewport(),QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.ShiftModifier,pos=position)
    assert left.isSelected() and right.isSelected()
