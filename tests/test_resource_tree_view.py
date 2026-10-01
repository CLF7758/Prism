from PyQt6.QtCore import Qt

from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import ResourceTreeModel
from prism.widgets.resource_tree_view import ResourceTreeView


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
