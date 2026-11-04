from unittest.mock import patch
from PyQt6 import QtCore, QtGui
from prism.asset_records import ImageRecord


def test_metadata_batches_coalesce_and_preserve_camera(main_window, qtbot):
    browser = main_window.asset_browser
    canvas = browser.list
    items = [ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 100}}, ('unused', i)) for i in range(2)]
    browser.model.replace(items)
    browser.timer.stop()
    main_window.show()
    canvas.show()
    qtbot.wait(30)
    canvas.setTransform(QtGui.QTransform.fromScale(1.7, 1.7))
    canvas.centerOn(QtCore.QPointF(300, 200))
    transform = canvas.transform()
    center = canvas.mapToScene(canvas.viewport().rect().center())
    with patch.object(canvas, '_arrange_contents', wraps=canvas._arrange_contents) as arrange:
        for row, item in enumerate(items):
            item._crop = QtCore.QRectF(0, 0, 30 + row, 90)
            browser.model.dataChanged.emit(browser.model.index(row), browser.model.index(row))
            expected_cells = set(canvas._cells(canvas.cards[row].sceneBoundingRect()))
            actual_cells = {cell for cell, rows in canvas._spatial.items() if row in rows}
            assert actual_cells == expected_cells
        assert arrange.call_count == 0
        qtbot.waitUntil(lambda: arrange.call_count == 1, timeout=1000)
    assert canvas.transform() == transform
    after = canvas.mapToScene(canvas.viewport().rect().center())
    assert abs(after.x() - center.x()) < 1
    assert abs(after.y() - center.y()) < 1
    assert canvas.model().items == items


def test_same_geometry_metadata_refreshes_only_changed_rows(main_window):
    browser = main_window.asset_browser
    canvas = browser.list
    items = [ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 100}}, ('unused', i)) for i in range(3)]
    browser.model.replace(items)
    browser.timer.stop()
    with patch.object(canvas, '_invalidate_display_frame') as full, patch.object(canvas, 'request_display_refresh') as local:
        browser.model.dataChanged.emit(browser.model.index(1), browser.model.index(1))
        full.assert_not_called()
        local.assert_called_once_with(items[1])
    assert canvas.model().items == items


def test_sparse_metadata_hook_visits_only_exact_rows(main_window):
    browser = main_window.asset_browser
    canvas = browser.list
    items = [ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 100}}, ('unused', i)) for i in range(100)]
    browser.model.replace(items)
    browser.timer.stop()
    from prism.widgets import asset_canvas
    with patch.object(asset_canvas, 'MaterialCard', wraps=asset_canvas.MaterialCard) as sized, patch.object(asset_canvas, '_card_content_size', wraps=asset_canvas._card_content_size) as measured, patch.object(canvas, 'request_display_refresh') as refresh:
        canvas.catalog_rows_changed((1, 98, 1))
        assert sized.call_count == 0
        assert [call.args[0] for call in measured.call_args_list] == [items[1], items[98]]
        assert [call.args[0] for call in refresh.call_args_list] == [items[1], items[98]]
