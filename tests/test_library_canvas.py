import pytest
from PyQt6 import QtCore, QtWidgets, QtTest
from tests.test_asset_browser import photo
from prism.workspace_service import ResourceTreeService
from prism.widgets.asset_canvas import MaterialCard


def test_empty_project_always_has_default_canvas(main_window):
    library = main_window.view.category_panel
    pages = library._pages_with_implicit_defaults()
    assert any(p['id'] == 'default-canvas' for p in pages)
    model = library.resource_tree.model()
    record = next(n for n in model._nodes.values() if n.get('pageId') == 'default-canvas')
    index = model.index_for_id(record['id'])
    assert index.isValid()
    menu = library.resource_tree.menu_for_index(index)
    assert not any(a.text() == '删除' for a in menu.actions())
    main_window._delete_resource_item(record['id'])
    assert model.index_for_id(record['id']).isValid()
    assert main_window.workspace_service.remove('default-canvas') == []


def test_default_canvas_cannot_be_deleted_through_parent_or_batch(qapp):
    service = ResourceTreeService('project')
    folder = service.createFolder('canvas', '文件夹')
    page = service.createPage('canvas', '默认画布', 'default-canvas', folder['id'])
    for root in (folder['id'], page['id']):
        assert not service.can_trash(root)
        with pytest.raises(ValueError):
            service.trash([root], '2026-10-13')
    assert all(n.get('deletedAt') is None for n in service.nodes)


def test_material_sections_use_same_toolbar_and_independent_canvas(main_window, qapp):
    item = photo(main_window, 'default-canvas', '素材')
    item.setPos(123, 456)
    window = main_window
    browser = window.asset_browser
    for mode in ('all', 'unclassified', 'untagged'):
        browser.open(mode)
        qapp.processEvents()
        assert isinstance(browser.list, QtWidgets.QGraphicsView)
        assert browser.list.scene() is not window.view.scene
        assert window._eagle_header_stack.currentWidget() is window.canvas_toolbar
        assert browser.model.items == [item]
        assert browser.list.cards[0].pos() == QtCore.QPointF(12, 12)
        browser.list.cards[0].setPos(900, 600)
        assert item.pos() == QtCore.QPointF(123, 456)
        browser.list.cards[0].setSelected(True)
        assert window.view._detail_panel._item is item
        window.canvas_toolbar.tools['pen'].click()
        assert browser.list.tool == 'pen'
        assert window.view.active_mode != window.view.DRAW_MODE
        window.canvas_toolbar.tools['select'].click()
        before = item.grayscale
        window.canvas_toolbar.gray.click()
        assert browser.list.display_grayscale
        assert item.grayscale == before
        window.canvas_toolbar.gray.click()
    browser.open('all')
    from prism.i18n import _
    assert browser.breadcrumb.text() == _('All assets')
    assert browser.list.sceneRect().width() > 100000
