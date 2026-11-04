from PyQt6 import QtWidgets

from prism.widgets.deferred_library import DeferredLibrary


def test_library_hosts_do_not_construct_hidden_pages(main_window):
    assert isinstance(main_window.model_library, DeferredLibrary)
    assert isinstance(main_window.material_library, DeferredLibrary)
    assert not main_window.model_library.initialized
    assert not main_window.material_library.initialized
    assert main_window.model_library.store_to_scene()
    assert main_window.material_library.store_to_scene()
    assert not main_window.model_library.initialized
    assert not main_window.material_library.initialized


def test_first_use_creates_once_and_keeps_inspector_host(qtbot):
    calls = []
    def factory():
        calls.append(True)
        page = QtWidgets.QWidget()
        page.details = QtWidgets.QWidget()
        page.details._pending_notes = None
        return page
    host = DeferredLibrary(factory)
    qtbot.addWidget(host)
    inspector = host.details
    first = host.ensure_created()
    assert host.ensure_created() is first
    assert host.details is inspector
    assert first.details.parentWidget() is inspector
    assert calls == [True]
    inspector._pending_notes = 'unsaved draft'
    assert first.details._pending_notes == 'unsaved draft'


def test_navigation_preservation_flag_reaches_real_page_before_and_after_creation(qtbot):
    def factory():
        page=QtWidgets.QWidget()
        page.details=QtWidgets.QWidget()
        return page
    host=DeferredLibrary(factory)
    qtbot.addWidget(host)
    host._preserve_editor_on_show=True
    page=host.ensure_created()
    assert page._preserve_editor_on_show is True
    host._preserve_editor_on_show=False
    assert page._preserve_editor_on_show is False
