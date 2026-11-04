"""A delayed layout must never mix or commit different grid generations."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtWidgets

from prism.widgets.asset_browser import AssetModel
from prism.widgets.asset_canvas import AssetCanvas


@pytest.fixture
def staged_canvas(qapp):
    owner = QtWidgets.QWidget()
    owner.mode = 'all'
    owner.view = SimpleNamespace(
        scene=SimpleNamespace(project_id='layout-test'),
        on_can_undo_changed=lambda _: None,
        on_can_redo_changed=lambda _: None)
    host = AssetCanvas(owner)
    model = AssetModel(owner)
    host.setModel(model)
    model.replace([SimpleNamespace(
        _image_size=QtCore.QSizeF(256, 128),
        crop=QtCore.QRectF(0, 0, 256, 128), _canvas_id='a')])
    # Keep scheduling deterministic; exercise the real slice entry points.
    with patch.object(QtCore.QTimer, 'singleShot'):
        host._start_staged_rebuild(keep_layout=True)
        yield host, host._build_state
    if hasattr(host, '_grid_timer'):
        host._grid_timer.stop()
    host._build_state['cancel'].set()
    owner.close()
    owner.deleteLater()


def test_grid_change_cancels_before_debounce(staged_canvas):
    host, state = staged_canvas
    original = QtCore.QSize(state['grid'])
    host.setGridSize(QtCore.QSize(original.width() + 1, original.height() + 1))
    assert host._grid_timer.isActive()
    assert state['cancel'].is_set()
    assert not host._build_current(state)
    assert state['grid'] == original


@pytest.mark.parametrize('boundary', ['collect', 'worker_reply', 'install'])
def test_old_geometry_generation_is_rejected_at_every_boundary(staged_canvas, boundary):
    host, state = staged_canvas
    original = tuple(host._geometry.x)
    host._geometry_version += 1
    with patch.object(host, '_commit_card_geometry') as commit:
        if boundary == 'collect':
            host._collect_layout_slice(state)
        elif boundary == 'worker_reply':
            host._layout_prepared((state, object(), None))
        else:
            host._install_layout_slice(state)
        commit.assert_not_called()
    assert state['cursor'] == 0
    assert 'prepared' not in state
    assert tuple(host._geometry.x) == original


def test_collection_uses_frozen_grid_and_accepts_current_generation(staged_canvas):
    host, state = staged_canvas
    assert host._build_current(state)
    with patch('prism.widgets.asset_canvas._card_content_size',
               return_value=QtCore.QSizeF(80, 40)) as measure:
        with patch.object(QtCore.QThreadPool, 'globalInstance'):
            host._collect_layout_slice(state)
    assert measure.call_args.args[1] is state['grid']
    assert list(state['content']) == [80, 40]
    assert state['cursor'] == 1
