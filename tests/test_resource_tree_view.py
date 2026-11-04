from PyQt6.QtCore import Qt

from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import (
    NODE_TYPE_ROLE, SECTION_ROLE, ResourceTreeModel)
from prism.widgets.resource_tree_view import ResourceTreeView


def _section_index(model, section):
    """The row of a section, by its section name."""
    from PyQt6 import QtCore
    root = QtCore.QModelIndex()
    for row in range(model.rowCount(root)):
        index = model.index(row, 0, root)
        if (index.data(NODE_TYPE_ROLE) == 'section'
                and index.data(SECTION_ROLE) == section):
            return index
    return QtCore.QModelIndex()


def test_replacing_the_model_keeps_the_open_categories(qtbot):
    """换模型不能把展开的分类收起来。

    Opening a project reinstalls the resource service, and that installs a
    new tree model.  A new model carries no expansion state, so every open
    category collapses -- the user sees the canvas category close itself in
    the middle of the open, and the interface jump with it.
    """
    service = ResourceTreeService('project')
    folder = service.createFolder('canvas', 'A')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.expand(_section_index(model, 'canvas'))
    view.expand(model.index_for_id(folder['id']))
    assert view.isExpanded(_section_index(model, 'canvas'))
    assert view.isExpanded(model.index_for_id(folder['id']))

    # The same nodes in a new model: the open categories stay open.
    replacement = ResourceTreeModel(ResourceTreeService('project', service.nodes))
    view.replace_model(replacement)
    assert view.isExpanded(_section_index(replacement, 'canvas'))
    assert view.isExpanded(replacement.index_for_id(folder['id']))


def test_a_model_reset_keeps_the_open_categories(qtbot):
    """模型重置也不能把展开的分类收起来。

    The first count refresh after a project opens adds the tag rows, and the
    service then resets the whole model.  The category that was open to show
    the current canvas collapses, which is the flash the user reports.
    """
    service = ResourceTreeService('project')
    folder = service.createFolder('canvas', 'A')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.replace_model(model)
    view.expand(_section_index(model, 'canvas'))
    view.expand(model.index_for_id(folder['id']))
    assert view.isExpanded(_section_index(model, 'canvas'))

    # A new node: the service reports it and the model resets itself.
    service.createFolder('canvas', 'B')
    assert view.isExpanded(_section_index(model, 'canvas'))
    assert view.isExpanded(model.index_for_id(folder['id']))


def test_section_blank_menu_routes_creation_without_last_selection(qtbot):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    requests = []
    view.create_requested.connect(lambda *args: requests.append(args))
    document = model.index(2, 0)
    blank = model.index(0, 0, document)
    menu = view.menu_for_index(blank)
    labels = [action.text() for action in menu.actions()]
    assert labels[:2] == ['新建文档', '新建文件夹']
    # 分区根行 / 空白行也能整棵导出（这一项以及它下面的全部层级）
    assert 'Export...' in labels
    menu.actions()[1].trigger()
    assert requests == [('document', 'folder', None)]


def test_folder_menu_routes_child_and_f2_rename(qtbot):
    service = ResourceTreeService('project')
    folder = service.createFolder('mindmap', 'A')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    requests = []
    view.create_requested.connect(lambda *args: requests.append(args))
    index = model.index_for_id(folder['id'])
    menu = view.menu_for_index(index)
    menu.actions()[0].trigger()
    assert requests == [('mindmap', 'page', folder['id'])]
    assert bool(model.flags(index) & Qt.ItemFlag.ItemIsEditable)


def test_unowned_gap_offers_all_sections(qtbot):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    menu = view.menu_for_index(model.index(-1, 0))
    labels = [action.text() for action in menu.actions()]
    assert '新建画布' in labels
    assert '新建脑图' in labels
    assert '新建文档' in labels
    assert '新建标签' in labels
