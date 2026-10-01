from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt

from prism.fileio.sql import SQLiteIO
from prism.items import PrismPixmapItem
from prism.workspace_service import ResourceTreeService


def test_workspace_pages_roundtrip(view, tmp_path):
    view.scene.workspace_pages = [
        {'id': 'doc-1', 'kind': 'document', 'title': '规划',
         'content': '<p>正文</p>', 'tags': []},
        {'id': 'map-1', 'kind': 'mindmap', 'title': '架构',
         'content': {'data': {'text': '中心'}, 'children': []}, 'tags': []},
    ]
    path = str(tmp_path / 'workspace-pages.prism')
    SQLiteIO(path, view.scene, create_new=True).write()

    view.scene.workspace_pages = []
    SQLiteIO(path, view.scene, readonly=True).read()
    # parent/permanent are part of a stored page now (sub-pages and the
    # generated legacy pages use them), so they come back alongside.
    assert view.scene.workspace_pages == [
        {'id': 'doc-1', 'kind': 'document', 'title': '规划',
         'content': '<p>正文</p>', 'tags': [],
         'parent': None, 'permanent': False},
        {'id': 'map-1', 'kind': 'mindmap', 'title': '架构',
         'content': {'data': {'text': '中心'}, 'children': []}, 'tags': [],
         'parent': None, 'permanent': False},
    ]
    assert view.scene.workspace_nodes == []


def test_resource_tree_roundtrip_with_deleted_nodes(view, tmp_path):
    original_project_id = view.scene.project_id
    tree = ResourceTreeService('project-1')
    folder = tree.createFolder('document', '资料夹')
    page = tree.createPage('document', '说明', 'doc-1', folder['id'])
    tree.trash([folder['id']], '2026-10-01T00:00:00Z')
    view.scene.workspace_nodes = tree.nodes
    path = str(tmp_path / 'resource-tree.prism')
    SQLiteIO(path, view.scene, create_new=True).write()

    view.scene.workspace_nodes = []
    SQLiteIO(path, view.scene, readonly=True).read()
    restored = ResourceTreeService('project-1', view.scene.workspace_nodes)
    assert view.scene.project_id == original_project_id
    assert {n['id'] for n in restored.nodes} == {folder['id'], page['id']}
    assert restored.listChildren('document') == []
    assert (restored.listChildren('document', include_deleted=True)[0]['id'] ==
            folder['id'])
    assert restored._by_id(page['id'])['parentId'] == folder['id']


def test_space_toggles_single_image_preview(view):
    image = QtGui.QImage(400, 200, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    item.setSelected(True)
    original = QtGui.QTransform(view.transform())

    press = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, Qt.Key.Key_Space,
                            Qt.KeyboardModifier.NoModifier)
    view.keyPressEvent(press)
    assert view.previous_transform is not None

    press = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, Qt.Key.Key_Space,
                            Qt.KeyboardModifier.NoModifier)
    view.keyPressEvent(press)
    assert view.previous_transform is None
    assert view.transform() == original
