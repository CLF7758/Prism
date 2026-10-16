"""Qt PDF output without a canvas-sized intermediate bitmap."""
from pathlib import Path
import tempfile
from PyQt6 import QtCore, QtGui, QtWidgets


def page_layout(width, height):
    # PDF's conventional maximum page extent is 200 inches (14400 points).
    # Reduce both axes together; the complete board keeps its aspect ratio.
    scale = min(1.0, 14000/max(width, height, 1))
    size = QtGui.QPageSize(QtCore.QSizeF(max(10, width*scale), max(10, height*scale)),
        QtGui.QPageSize.Unit.Point, 'Prism')
    return QtGui.QPageLayout(size, QtGui.QPageLayout.Orientation.Portrait,
        QtCore.QMarginsF(0, 0, 0, 0), QtGui.QPageLayout.Unit.Point)


def write_bytes(path, data):
    output = QtCore.QSaveFile(str(path))
    if not output.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    if output.write(data) != len(data) or not output.commit():
        raise OSError(output.errorString())


def export_scene_pdf(scene, path):
    """Render all visible saved items, excluding task cards and selection UI.

    Runs on the GUI thread: QGraphicsScene/QPixmap must never be painted from
    a ThreadedIO worker. Virtual records decode their originals one at a time.
    """
    scene.cancel_active_modes()
    items = [i for i in scene.items_for_save() if i.isVisible() and not getattr(i, '_deleted_at', None)]
    if not items:
        raise ValueError('当前画布没有可导出的内容')
    rect = scene.itemsBoundingRect(items=items)
    margin = max(rect.width(), rect.height(), 1)*.03
    source = rect.adjusted(-margin, -margin, margin, margin)
    output = QtCore.QSaveFile(str(path))
    if not output.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    writer = QtGui.QPdfWriter(output)
    writer.setCreator('Prism')
    writer.setResolution(96)
    writer.setPageLayout(page_layout(source.width()*.75, source.height()*.75))
    painter = QtGui.QPainter()
    hidden, selected = [], []
    try:
        if not painter.begin(writer):
            raise OSError('无法创建 PDF 文件')
        for item in scene.selectedItems(user_only=True):
            item.setSelected(False)
            selected.append(item)
        # Overlays do not have save_id. Keep record drawing layers, which
        # represent saved logical assets rather than overlay controls.
        for item in QtWidgets.QGraphicsScene.items(scene):
            if not hasattr(item, 'save_id') and not getattr(item, '_is_record_layer', False) and item.isVisible():
                item.setVisible(False)
                hidden.append(item)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        target = QtCore.QRectF(0, 0, writer.width(), writer.height())
        painter.fillRect(target, QtGui.QColor('#1c1d1f'))
        scene.render(painter, target=target, source=source)
        painter.end()
        del writer
        if not output.commit():
            raise OSError(output.errorString())
    except Exception:
        if painter.isActive():
            painter.end()
        output.cancelWriting()
        raise
    finally:
        for item in hidden:
            item.setVisible(True)
        for item in selected:
            item.setSelected(True)


MINDMAP_MARKUP = r'''(() => {
    const map = window.__mindMap;
    if (!map || !map.draw || !map.renderer.root) return null;
    const original = map.draw.node;
    const box = original.getBBox();
    if (!box.width || !box.height) return null;
    const group = original.cloneNode(true);
    group.removeAttribute('transform');
    // Print resolved typography, including foreignObject rich-text nodes.
    const from = [original, ...original.querySelectorAll('*')];
    const to = [group, ...group.querySelectorAll('*')];
    from.forEach((element, index) => {
        const css = getComputedStyle(element);
        for (const key of ['font-family','font-size','font-weight','font-style',
                           'color','line-height','white-space','text-align',
                           'margin','padding','display','visibility','opacity',
                           'letter-spacing','word-spacing','text-decoration','overflow'])
            to[index].style.setProperty(key, css.getPropertyValue(key));
    });
    group.querySelectorAll('.smm-node-hover,.smm-node-active,.smm-expand-btn').forEach(n => n.remove());
    const pad = 24, width = box.width+pad*2, height = box.height+pad*2;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    svg.setAttribute('viewBox', `${box.x-pad} ${box.y-pad} ${width} ${height}`);
    svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
    svg.appendChild(group);
    return {width, height, svg: new XMLSerializer().serializeToString(svg)};
})()'''


def export_mindmap_pdf(panel, path, finished):
    """Print the complete upstream SVG, independent of pan/zoom, asynchronously."""
    from PyQt6 import QtWebEngineCore
    if not panel.web_view or not panel._ready:
        raise ValueError('请等待脑图加载完成后再导出')
    if getattr(panel, '_pdf_export_page', None) is not None:
        raise ValueError('脑图 PDF 正在导出，请稍候')
    state = {'done': False}
    timer = QtCore.QTimer(panel)
    timer.setSingleShot(True)
    def finish(error=None):
        if state['done']:
            return
        state['done'] = True
        timer.stop()
        timer.deleteLater()
        page = getattr(panel, '_pdf_export_page', None)
        panel._pdf_export_page = None
        if page:
            page.deleteLater()
        temporary = state.pop('temporary', None)
        if temporary:
            temporary.cleanup()
        finished(error)
    timer.timeout.connect(lambda: finish('脑图 PDF 导出超时'))
    timer.start(60000)
    # Mark the request busy even before JavaScript answers.
    panel._pdf_export_page = QtWebEngineCore.QWebEnginePage(panel)
    def received(data):
        if state['done']:
            return
        if not isinstance(data, dict) or not data.get('svg'):
            finish('无法取得完整脑图，请稍后再试')
            return
        page = panel._pdf_export_page
        layout = page_layout(float(data['width'])*.75, float(data['height'])*.75)
        def printed(buffer):
            if state['done']:
                return
            try:
                if not buffer or not bytes(buffer).startswith(b'%PDF'):
                    raise OSError('脑图 PDF 渲染失败')
                write_bytes(path, buffer)
            except (OSError, ValueError) as error:
                finish(str(error))
            else:
                finish()
        def loaded(ok):
            if state['done']:
                return
            if not ok:
                finish('脑图 PDF 页面加载失败')
                return
            page.printToPdf(printed, layout)
        page.loadFinished.connect(loaded)
        html = '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:100%;height:100%;background:#1c1c1e}svg{display:block;position:absolute;inset:0}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}</style></head><body>'+data['svg']+'</body></html>'
        # setHtml uses a data URL and fails for larger maps (~2 MiB). A
        # private temporary file supports large SVGs without that URL limit.
        try:
            state['temporary'] = tempfile.TemporaryDirectory(prefix='prism-mindmap-pdf-', ignore_cleanup_errors=True)
            source = Path(state['temporary'].name)/'map.html'
            source.write_text(html, encoding='utf-8')
            page.load(QtCore.QUrl.fromLocalFile(str(source)))
        except OSError as error:
            finish(str(error))
    panel.web_view.page().runJavaScript(MINDMAP_MARKUP, received)
