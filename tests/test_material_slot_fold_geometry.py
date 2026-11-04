"""Exercise the intermediate layout before the parent shrinks a folded slot."""
import pytest
from PyQt6 import QtCore, QtWidgets

from prism.material_parameters import RANGES
from prism.widgets.material_slots import MaterialSlot


@pytest.mark.parametrize('key', RANGES)
def test_fold_heading_stays_at_top_before_parent_resize(qtbot, key):
    slot = MaterialSlot(key, key)
    qtbot.addWidget(slot)
    slot.fold.setChecked(True)
    slot.resize(297, slot.sizeHint().height())
    slot.show()
    qtbot.waitExposed(slot)
    # Hold the old expanded geometry, as happens between the child's layout
    # request and the scroll area's later resize. Paint that intermediate state.
    slot.setFixedHeight(slot.height())
    heading_y = slot.fold.y()
    expanded_height = slot.height()
    for _ in range(3):
        slot.fold.setChecked(False)
        slot.layout().activate()
        slot.grab()
        assert slot.height() == expanded_height
        assert slot.fold.y() == heading_y
        assert slot.enabled.y() == heading_y
        slot.fold.setChecked(True)
        slot.layout().activate()
        assert slot.fold.y() == heading_y
    slot.setMinimumHeight(0)
    slot.setMaximumHeight(QtWidgets.QWIDGETSIZE_MAX)
    slot.fold.setChecked(False)
    slot.resize(slot.width(), slot.sizeHint().height())
    slot.layout().activate()
    assert slot.height() < expanded_height
    assert slot.fold.y() == heading_y


@pytest.mark.parametrize('position', ['top', 'middle', 'bottom'])
@pytest.mark.parametrize('key', RANGES)
def test_details_fold_updates_layout_and_scroll_range_before_return(qtbot, monkeypatch, position, key):
    from prism.widgets import material_render
    from prism.widgets.material_details import MaterialDetails
    # This is a layout test: leave the real scroll/form/slots intact, omit GPU.
    monkeypatch.setattr(material_render, 'MaterialRender', QtWidgets.QWidget)
    details = MaterialDetails()
    qtbot.addWidget(details)
    details.empty.hide()
    details.header.show()
    details.sections.show()
    details.footer.show()
    details.slots[key].fold.setChecked(True)
    details.resize(400, 700)
    details.show()
    qtbot.waitExposed(details)
    qtbot.wait(30)
    bar = details.scroll.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue({'top': 0, 'middle': bar.maximum() * 3 // 4,
                  'bottom': bar.maximum()}[position])
    slot = details.slots[key]
    old_height, old_max, old_value = slot.height(), bar.maximum(), bar.value()
    assert slot.fold.isChecked()
    slot.fold.setChecked(False)
    # No event processing here: the state must be coherent before any Paint.
    assert slot.height() == slot.sizeHint().height()
    removed = old_height - slot.height()
    assert removed > 0
    assert bar.maximum() == old_max - removed
    assert bar.value() == min(old_value, bar.maximum())
    collapsed_max = bar.maximum()
    slot.fold.setChecked(True)
    assert slot.height() == old_height
    assert bar.maximum() == collapsed_max + removed


@pytest.mark.parametrize('position', ['top', 'middle', 'bottom'])
def test_fold_paints_only_final_geometry_and_leaves_metadata_scroll_alone(qtbot, monkeypatch, position):
    from prism.widgets import material_render
    from prism.widgets.material_details import MaterialDetails
    monkeypatch.setattr(material_render, 'MaterialRender', QtWidgets.QWidget)
    details = MaterialDetails()
    qtbot.addWidget(details)
    details.empty.hide()
    details.header.show()
    details.sections.show()
    details.footer.show()
    details.resize(400, 700)
    details.show()
    qtbot.waitExposed(details)
    qtbot.wait(30)
    bar = details.scroll.verticalScrollBar()
    bar.setValue({'top': 0, 'middle': bar.maximum() * 3 // 4,
                  'bottom': bar.maximum()}[position])
    qtbot.wait(10)
    metadata = details.metadata_scroll
    metadata_before = (metadata.geometry(), metadata.widget().geometry(),
                       metadata.verticalScrollBar().maximum(), metadata.verticalScrollBar().value())

    def geometry():
        return (details.form.height(), details.form.sizeHint().height(),
                details.scroll.viewport().size(), bar.maximum(), bar.value(),
                tuple((s.geometry(), s.fold.geometry()) for s in details.slots.values()))

    snapshots = []
    watched = {details.form, details.scroll.viewport(), *details.slots.values()}

    class PaintObserver(QtCore.QObject):
        def eventFilter(self, watched_object, event):
            if watched_object in watched and event.type() == QtCore.QEvent.Type.Paint:
                snapshots.append(geometry())
            return False

    observer = PaintObserver()
    app = QtWidgets.QApplication.instance()
    app.installEventFilter(observer)
    try:
        for expanded in (False, True, False):
            snapshots.clear()
            details.slots['roughness'].fold.setChecked(expanded)
            expected = geometry()
            qtbot.waitUntil(lambda: bool(snapshots))
            qtbot.wait(30)
            assert all(snapshot == expected for snapshot in snapshots)
            assert geometry() == expected
        assert (metadata.geometry(), metadata.widget().geometry(),
                metadata.verticalScrollBar().maximum(), metadata.verticalScrollBar().value()) == metadata_before
    finally:
        app.removeEventFilter(observer)
