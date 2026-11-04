from PyQt6 import QtCore, QtGui
import pytest

from prism.widgets.asset_grid import AssetGrid


def test_large_metadata_refresh_does_not_walk_every_visual_rectangle(qtbot, monkeypatch):
    grid = AssetGrid()
    qtbot.addWidget(grid)
    model = QtGui.QStandardItemModel(20000, 1, grid)
    grid.setModel(model)
    def unexpected(*args):
        raise AssertionError('A fixed-size grid must not walk 20k dirty rectangles')
    monkeypatch.setattr(grid, 'visualRect', unexpected)
    grid.dataChanged(model.index(0, 0), model.index(19999, 0))


@pytest.mark.parametrize('width', [90, 347, 813, 1803])
def test_grid_uses_full_width_and_hit_testing_matches_cells(qtbot, width):
    grid = AssetGrid()
    qtbot.addWidget(grid)
    model = QtGui.QStandardItemModel(40, 1, grid)
    grid.setModel(model)
    grid.resize(width, 400)
    grid.show()
    columns = grid.columns()
    previous_right = -1
    for column in range(columns):
        rect = grid.visualRect(model.index(column, 0))
        assert rect.left() == previous_right + 1
        for x in (rect.left(), rect.center().x(), rect.right()):
            assert grid.indexAt(QtCore.QPoint(x, 1)).row() == column
        grid.setSelection(rect, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        assert [index.row() for index in grid.selectionModel().selectedIndexes()] == [column]
        previous_right = rect.right()
    assert previous_right == grid.viewport().width() - 1



def test_native_selection_does_not_reveal_an_already_clicked_card(qtbot, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    grid = AssetGrid()
    qtbot.addWidget(grid)
    model = QtGui.QStandardItemModel(30, 1, grid)
    grid.setModel(model)
    run = Mock()
    grid.native_cards = SimpleNamespace(isVisible=lambda: True, ready=True,
        web=SimpleNamespace(page=lambda: SimpleNamespace(runJavaScript=run)), _dom_id=str)
    grid._native_rows = {'clicked': 20}
    monkeypatch.setattr(grid, '_resource_id', lambda item: 'clicked')
    grid.selectionModel().currentChanged.connect(lambda current, previous: grid.scrollTo(current))
    grid._native_select('clicked', False)
    grid._native_select_many(['clicked'], 'clicked', False)
    run.assert_not_called()
    grid.scrollTo(model.index(20, 0))
    assert run.call_count == 1  # explicit navigation still reveals the card
    grid.native_cards = None
