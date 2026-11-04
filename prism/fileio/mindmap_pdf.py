"""Full-tree vector PDF with tiled pages; never capture the live viewport.

This print layout includes folded branches without changing editor state.
It deliberately paginates a readable branch diagram instead of squeezing the
whole tree into a tiny screenshot. Original-file exports are separate.
"""
import html
import math
from pathlib import Path
from PyQt6 import QtCore, QtGui

from prism.fileio.attachments import attachment_for
from prism.fileio.mindmap_export import MindMapExportError, children_of, node_note, node_text
from prism.i18n import _


def write_mindmap_pdf(tree, path, attachments=None, *, cancel=None):
    def check():
        if cancel is not None and cancel():
            raise MindMapExportError(_('Export cancelled'))

    font = QtGui.QFont('Microsoft YaHei', 10)
    document = QtGui.QTextDocument()
    document.setDefaultFont(font)
    document.setDocumentMargin(0)
    nodes = []
    stack = [(tree, 0, None)]
    seen = set()
    y = 0.0
    while stack:
        check()
        node, depth, parent = stack.pop()
        if id(node) in seen:
            raise MindMapExportError(_('The mind map contains repeated objects or cyclic references.'))
        seen.add(id(node))
        data = node.get('data', {})
        title = node_text(node)
        parts = ['<b>'+html.escape(title)+'</b>']
        note = node_note(node)
        if note:
            parts.append(html.escape(note))
        tags = data.get('tag')
        if tags:
            parts.append(html.escape(', '.join(map(str,tags)) if isinstance(tags,list) else str(tags)))
        if data.get('hyperlink'):
            parts.append(html.escape(str(data['hyperlink'])))
        content = '<br/>'.join(parts)
        document.setTextWidth(216)
        document.setHtml(content)
        text_height = document.size().height()
        image = data.get('image')
        image_height = 0
        if image:
            entry = attachment_for(image, attachments)
            if entry is None or not entry[2]:
                raise MindMapExportError(_('Mind map image original is missing: {title}').format(title=title))
            buffer = QtCore.QBuffer()
            buffer.setData(entry[2])
            buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
            size = QtGui.QImageReader(buffer).size()
            if not size.isValid() or size.isEmpty():
                raise MindMapExportError(_('Mind map image cannot be read: {title}').format(title=title))
            image_height = min(140.0,216.0*size.height()/size.width())+8
        rect = QtCore.QRectF(depth*280.0, y, 240.0, text_height+image_height+24)
        index = len(nodes)
        nodes.append((rect,parent,content,text_height,image,image_height))
        y += rect.height()+28
        for child in reversed(children_of(node)):
            stack.append((child,depth+1,index))
    if not nodes:
        raise MindMapExportError(_('There is no mind map to export yet.'))

    writer = QtGui.QPdfWriter(str(path))
    writer.setResolution(96)
    writer.setPageSize(QtGui.QPageSize(QtGui.QPageSize.PageSizeId.A4))
    writer.setPageOrientation(QtGui.QPageLayout.Orientation.Landscape)
    writer.setPageMargins(QtCore.QMarginsF(12,12,12,12),QtGui.QPageLayout.Unit.Millimeter)
    writer.setTitle(node_text(tree))
    writer.setCreator('Prism')
    painter = QtGui.QPainter(writer)
    if not painter.isActive():
        raise MindMapExportError(_('Could not create the mind map PDF.'))
    width, height = float(writer.width()),float(writer.height()-28)
    columns = max(1,math.ceil(max(row[0].right() for row in nodes)/width))
    rows = max(1,math.ceil(y/height))
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.LosslessImageRendering)
        painter.setFont(font)
        for row in range(rows):
            for column in range(columns):
                check()
                if row or column:
                    if not writer.newPage():
                        raise MindMapExportError(_('Could not write a mind map PDF page.'))
                painter.save()
                painter.setClipRect(QtCore.QRectF(0,0,width,height))
                painter.translate(-column*width,-row*height)
                tile = QtCore.QRectF(column*width,row*height,width,height)
                for rect,parent,content,text_height,image,image_height in nodes:
                    check()
                    if parent is not None:
                        origin = nodes[parent][0]
                        line = QtGui.QPainterPath(QtCore.QPointF(origin.right(),origin.center().y()))
                        bend = (origin.right()+rect.left())/2
                        line.lineTo(bend,origin.center().y())
                        line.lineTo(bend,rect.center().y())
                        line.lineTo(rect.left(),rect.center().y())
                        if line.boundingRect().intersects(tile):
                            painter.setPen(QtGui.QPen(QtGui.QColor('#777777'),1))
                            painter.drawPath(line)
                    if not rect.intersects(tile):
                        continue
                    painter.setPen(QtGui.QPen(QtGui.QColor('#577293'),1))
                    painter.setBrush(QtGui.QColor('#f3f6fb'))
                    painter.drawRoundedRect(rect,6,6)
                    painter.save()
                    painter.translate(rect.left()+12,rect.top()+12)
                    document.setHtml(content)
                    document.drawContents(painter)
                    if image:
                        blob = attachment_for(image,attachments)[2]
                        picture = QtGui.QImage.fromData(blob)
                        if picture.isNull():
                            raise MindMapExportError(_('Could not render a mind map image; export stopped.'))
                        scale = min(216/picture.width(),(image_height-8)/picture.height())
                        painter.drawImage(QtCore.QRectF(0,text_height+8,picture.width()*scale,picture.height()*scale),picture)
                    painter.restore()
                painter.restore()
                painter.setPen(QtGui.QColor('#555555'))
                painter.drawText(QtCore.QRectF(0,height+4,width,22),QtCore.Qt.AlignmentFlag.AlignRight,
                    f'{row*columns+column+1} / {rows*columns}  ({column+1}, {row+1})')
    finally:
        ended = painter.end()
    if not ended or not Path(path).is_file() or Path(path).stat().st_size == 0:
        raise MindMapExportError(_('The mind map PDF was not completely written.'))
    return str(path)
