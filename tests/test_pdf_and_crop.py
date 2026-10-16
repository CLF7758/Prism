from pathlib import Path
from PyQt6 import QtCore, QtGui
from prism.fileio.pdf_export import export_scene_pdf
from prism.items import PrismPixmapItem


def test_image_context_crop_commit_undo_keeps_original(view, qtbot):
    image = QtGui.QImage(200, 120, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    item._canvas_id = 'default-canvas'
    view.scene.addItem(item)
    item.setSelected(True)
    view._inject_canvas_tools()
    assert view._crop_image_action.text() == '裁切图片'
    assert view._crop_image_action.isEnabled()
    original = QtCore.QRectF(item.crop)
    view._crop_image_action.trigger()
    assert view.scene.crop_item is item
    item.crop_temp = QtCore.QRectF(20, 10, 80, 60)
    item.exit_crop_mode(True)
    assert item.crop == QtCore.QRectF(20, 10, 80, 60)
    view.undo_stack.undo()
    assert item.crop == original
    view.undo_stack.redo()
    assert item.crop.width() == 80
    assert item._image_size == image.size()


def test_canvas_pdf_has_real_content_and_restores_selection(view, tmp_path):
    from PyQt6 import QtPdf
    image = QtGui.QImage(200, 120, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    item._canvas_id = 'default-canvas'
    view.scene.addItem(item)
    item.setSelected(True)
    from prism.actions.video_download_workflow import controller
    manager = controller(view)
    node = manager.create(QtCore.QPointF(800, 100))
    path = tmp_path/'canvas.pdf'
    export_scene_pdf(view.scene, path)
    assert item.isSelected() and node.isVisible()
    doc = QtPdf.QPdfDocument(None)
    assert doc.load(str(path)) == QtPdf.QPdfDocument.Error.None_
    assert doc.pageCount() == 1
    rendered = doc.render(0, QtCore.QSize(400, 250))
    assert not rendered.isNull()
    color = rendered.pixelColor(200, 125)
    assert color.red() > 200 and color.green() < 50
    doc.close()
    manager.remove(manager.nodes[0])
    manager.service.close()


def test_empty_canvas_pdf_does_not_replace_existing_file(view, tmp_path):
    import pytest
    path = tmp_path/'existing.pdf'
    path.write_bytes(b'original')
    with pytest.raises(ValueError):
        export_scene_pdf(view.scene, path)
    assert path.read_bytes() == b'original'


def test_format_dropdown_replaces_default_extension():
    from prism.actions.export_workflow import selected_export_path
    assert selected_export_path('note.docx', 'PDF (*.pdf)', ('pdf', 'docx')) == 'note.pdf'
    assert selected_export_path('map.xmind', 'PDF (*.pdf)', ('pdf', 'xmind')) == 'map.pdf'
    assert selected_export_path('board', 'PDF (*.pdf)', ('pdf', 'png')) == 'board.pdf'
    assert selected_export_path('board.v2', 'PDF (*.pdf)', ('pdf', 'png')) == 'board.v2.pdf'
    assert selected_export_path('board.jpg', 'Image Files (*.png *.jpg *.jpeg *.svg)', ('png', 'jpg', 'jpeg', 'svg', 'pdf')) == 'board.jpg'


def test_real_mindmap_pdf(view, qtbot, tmp_path):
    import os
    import pytest
    if os.environ.get('PRISM_WEBENGINE_ACCEPTANCE') != '1':
        pytest.skip('Explicit isolated Chromium acceptance')
    from PyQt6 import QtPdf
    from prism.widgets.mindmap_panel import MindMapPanel
    from prism.fileio.pdf_export import export_mindmap_pdf
    view.scene.mindmap_tree = dict(data=dict(text='Prism 中文导出'), children=[
        dict(data=dict(text='第一分支 画布'), children=[]),
        dict(data=dict(text='第二分支 文档'), children=[])])
    panel = MindMapPanel(view)
    qtbot.addWidget(panel)
    panel.resize(1000, 700)
    panel.show()
    panel.load_from_scene()
    qtbot.waitUntil(lambda: panel._ready, timeout=30000)
    ready = []
    def rendered():
        panel.web_view.page().runJavaScript('Boolean(window.__mindMap && window.__mindMap.renderer.root)', lambda value: ready.append(value))
    rendered()
    def is_rendered():
        if ready and ready[-1]:
            return True
        rendered()
        return False
    qtbot.waitUntil(is_rendered, timeout=30000)
    moved = []
    panel.web_view.page().runJavaScript("window.__mindMap.draw.node.setAttribute('transform','translate(8000 8000) scale(3)'); true", moved.append)
    qtbot.waitUntil(lambda: bool(moved), timeout=5000)
    path = Path(os.environ.get('PRISM_ACCEPTANCE_OUTPUT', str(tmp_path)))/'mindmap.pdf'
    path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    export_mindmap_pdf(panel, path, results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=65000)
    assert results == [None], results
    doc = QtPdf.QPdfDocument(None)
    assert doc.load(str(path)) == QtPdf.QPdfDocument.Error.None_
    assert doc.pageCount() == 1
    text = doc.getAllText(0).text()
    assert 'Prism' in text and '第一分支' in text and '第二分支' in text, text
    size = doc.pagePointSize(0)
    doc.render(0, QtCore.QSize(1200, round(1200*size.height()/size.width()))).save(str(path.with_suffix('.png')))
    doc.close()
    panel.close()
