"""Own canvas download nodes and destination guards outside the main view."""
from pathlib import Path
from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.video_download_service import service_for
from prism.widgets.video_download_node import DownloadNode


class InsertDownloadedVideo(QtGui.QUndoCommand):
    """Background completion must not deselect the user's current work."""
    def __init__(self, view, item):
        super().__init__('下载视频到画布')
        self.view, self.item = view, item
        self.items = item if isinstance(item, list) else [item]

    def redo(self):
        for item in self.items:self.view.scene.addItem(item)
        self.view.category_panel._apply_filter()

    def undo(self):
        for item in self.items:self.view.scene.removeItem(item)


class CanvasDownloads(QtCore.QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.service = service_for(view)
        self.nodes = []
        self.service.progress.connect(self.progress)
        self.service.finished.connect(self.finished)
        self.service.failed.connect(self.failed)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.sync)
        app = QtWidgets.QApplication.instance()
        app.aboutToQuit.connect(self.service.close)

    def create(self, position):
        if len(self.nodes) >= 20:
            from prism.widgets import PrismNotification
            PrismNotification(self.view, '最多保留 20 个下载节点，请先移除已完成的节点')
            return None
        if self.view._material_canvas() is not None:
            from prism.widgets import PrismNotification
            PrismNotification(self.view, '请先打开一个具体画布，再创建下载节点')
            return None
        canvas = self.view._ensure_current_canvas()
        node = DownloadNode()
        entry = dict(node=node, context=dict(project_id=self.view.scene.project_id,
            canvas_id=canvas, x=position.x(), y=position.y()), job=None, result=None)
        self.nodes.append(entry)
        self.view.scene.addItem(node)
        node.setPos(position)
        node.start_requested.connect(lambda: self.submit(entry))
        node.available_quality_requested.connect(lambda: self.submit(entry, 'best_available'))
        node.session_requested.connect(lambda: self.choose_session(entry))
        node.cancel_requested.connect(lambda: self.service.cancel(entry['job']))
        node.remove_requested.connect(lambda: self.remove(entry))
        node.import_requested.connect(lambda: self.import_video(entry))
        node.url.setFocus()
        self.timer.start()
        return node

    def remove(self, entry, cancel=True):
        if entry not in self.nodes:
            return
        if cancel:
            self.service.cancel(entry['job'])
        node = entry['node']
        self.nodes.remove(entry)
        if not sip.isdeleted(node):
            if node.scene():
                self.view.scene.removeItem(node)
            node.deleteLater()
        if not self.nodes:
            self.timer.stop()

    def choose_session(self, entry):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self.view, '选择 B站 Netscape 会话文件', '', '文本文件 (*.txt);;所有文件 (*)')
        if filename:
            entry['cookie_file'] = filename
            self.submit(entry)

    def submit(self, entry, quality_policy='original'):
        node = entry['node']
        if quality_policy == 'best_available' and entry.get('limited_url') != node.url.text():
            return
        if entry['job'] in self.service.running or entry['job'] in self.service.queue:
            return
        entry['result'] = None
        try:
            node.busy(True)
            entry['context'].update(x=node.pos().x(), y=node.pos().y())
            identifier = self.service.submit(node.url.text(), Path.home()/'Downloads/Prism', context=entry['context'], retain_original=True,
                cookie_file=entry.get('cookie_file'), quality_policy=quality_policy,
                minimum_dimensions=entry.get('limited_available') if quality_policy == 'best_available' else None)
        except (ValueError, OSError) as error:
            node.terminal(str(error))
            return
        entry['job'] = identifier

    def find(self, identifier):
        return next((entry for entry in self.nodes if entry['job'] == identifier and not sip.isdeleted(entry['node'])), None)

    def progress(self, identifier, data):
        entry = self.find(identifier)
        if entry:
            entry['node'].progress(data)

    def failed(self, identifier, message):
        entry = self.find(identifier)
        if entry:
            entry['node'].terminal('下载失败：'+message)
            result = self.service.jobs.get(identifier, {}).get('result') or {}
            if result.get('code') == 'quality_limited':
                entry['limited_url'] = entry['node'].url.text()
                entry['limited_available'] = result.get('available')
                entry['node'].quality_limited(result.get('available') or {})

    def finished(self, identifier, result):
        entry = self.find(identifier)
        if not entry:
            return
        node = entry['node']
        if result['event'] != 'completed':
            node.terminal('已取消')
            return
        entry['result'] = result
        node.terminal('下载完成：'+result.get('title', ''), True)
        node.import_button.show()
        # Insertion is tiny and asynchronous media decoding is owned by the
        # existing video item. Defer while a legacy import macro is open.
        self.import_video(entry)

    def valid_destination(self, entry):
        context = entry['context']
        scene = self.view.scene
        resources = getattr(self.view.category_panel, 'resource_service', None)
        if resources and any(n.get('pageId') == context['canvas_id'] and n.get('deletedAt') for n in resources.nodes):
            return False
        return context['project_id'] == scene.project_id and (
            context['canvas_id'] == 'default-canvas' or any(
                p.get('id') == context['canvas_id'] for p in scene.workspace_pages))

    def import_video(self, entry):
        result, node = entry['result'], entry['node']
        if not result or result.get('imported'):
            return
        if not self.valid_destination(entry):
            node.status.setText('目标画布已不存在；视频已保存在下载目录')
            return
        interacting = (node.dragging or self.view.scene.active_mode is not None or
            self.view.active_mode in (self.view.PAN_MODE, self.view.ZOOM_MODE) or
            getattr(self.view, '_draw_item', None) is not None)
        if (getattr(self.view, '_import_in_progress', False) or self.view.scene.crop_item or
                self.view.scene.edit_item or interacting or QtWidgets.QApplication.activeModalWidget()):
            node.status.setText('下载完成；编辑结束后自动放入原画布')
            return
        from prism.items import PrismVideoItem, PrismPixmapItem
        from prism.compact_layout import pack_sizes
        import math
        context = entry['context']
        context.update(x=node.pos().x(), y=node.pos().y())
        assets = result.get('assets') or [dict(result, asset_type='video')]
        items=[]
        for asset in assets:
            path=asset.get('original_path') or asset['path']
            if asset['asset_type']=='image':
                item=PrismPixmapItem(QtGui.QImage(),Path(asset['path']).name)
                item._source_path=path
                size=asset['image']
                item._image_size=QtCore.QSize(size['width'],size['height'])
                item._source_format=size.get('format')
                item._preview_pixmap=QtGui.QPixmap(asset['preview_path'])
                item.reset_crop()
            else:
                item=PrismVideoItem(QtCore.QUrl.fromLocalFile(path),Path(asset['path']).name)
                item._video_source_path=path
                video=next((s for s in asset.get('media',{}).get('streams',[]) if s.get('codec_type')=='video'),{})
                if video.get('width') and video.get('height'):
                    item._video_dimensions=QtCore.QSize(video['width'],video['height'])
            item._title=result.get('title','')
            item._canvas_id=context['canvas_id']
            items.append(item)
        sizes=[(max(1,math.ceil(i.bounding_rect_unselected().width()+24)),max(1,math.ceil(i.bounding_rect_unselected().height()+24))) for i in items]
        positions,_=pack_sizes(sizes)
        for item,(x,y) in zip(items,positions):
            item.setPos(QtCore.QPointF(context['x']+x,context['y']+y))
        self.view.undo_stack.push(InsertDownloadedVideo(self.view,items))
        result['imported'] = True
        self.remove(entry, cancel=False)

    def sync(self):
        for entry in list(self.nodes):
            node = entry['node']
            if sip.isdeleted(node) or entry['context']['project_id'] != self.view.scene.project_id:
                self.remove(entry)
                continue
            node.setVisible(self.view._material_canvas() is None and
                self.view.current_canvas_id == entry['context']['canvas_id'])
            if entry['result'] and not entry['result'].get('imported'):
                self.import_video(entry)


def controller(view):
    result = getattr(view, '_canvas_downloads', None)
    if result is None:
        result = view._canvas_downloads = CanvasDownloads(view)
    return result


def create(view, position):
    return controller(view).create(position)
