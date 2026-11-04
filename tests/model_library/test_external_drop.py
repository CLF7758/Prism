"""External drops must reach the model import service, not Chromium navigation."""
from PyQt6 import QtCore, QtGui, QtWidgets
from tests.model_library.test_ui import (
    FakeService, FakeRepository, ModelCapabilities, page, host, _wait_until_settled,
)


def test_drop_on_web_child_imports_once_and_keeps_internal_moves(page, qtbot, tmp_path, monkeypatch):
    service = FakeService(FakeRepository(1), ModelCapabilities(browse=True, import_models=True))
    page.set_service(service)
    page.bind_scope('project-a', 1)
    _wait_until_settled(qtbot, page)
    qtbot.waitUntil(lambda: page.cards is not None and page.import_button.isEnabled())
    child = QtWidgets.QWidget(page.cards.web)
    source = tmp_path / 'test.FBX'
    source.write_bytes(b'test')
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(source))]*2)

    def event(kind):
        if kind == 'drop':
            return QtGui.QDropEvent(QtCore.QPointF(10,10), QtCore.Qt.DropAction.CopyAction,
                mime, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
        return QtGui.QDragEnterEvent(QtCore.QPoint(10,10), QtCore.Qt.DropAction.CopyAction,
            mime, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)

    enter=event('enter')
    assert page.cards.eventFilter(child,enter) and enter.isAccepted()
    assert service.calls == []
    drop=event('drop')
    assert page.cards.eventFilter(child,drop) and drop.isAccepted()
    assert service.calls == [('import_paths',(source.as_posix(),),None)]
    mime.setData('application/x-prism-library-assets', b'{}')
    assert not page.cards.eventFilter(child,event('drop'))
    assert len(service.calls)==1
    mime.removeFormat('application/x-prism-library-assets')
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(tmp_path/'unsupported.txt'))])
    rejected=event('drop')
    assert page.cards.eventFilter(child,rejected) and not rejected.isAccepted()
    assert len(service.calls)==1
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(tmp_path))])
    monkeypatch.setattr('os.listdir',lambda path: (_ for _ in ()).throw(AssertionError('Drag must not enumerate folders')))
    assert page._accept_model_drop(mime)
