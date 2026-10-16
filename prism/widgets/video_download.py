"""Thin download UI; platform extraction and decoding live outside Qt's UI."""
from pathlib import Path
from types import SimpleNamespace
from PyQt6 import QtCore, QtWidgets
from prism.video_download_service import service_for


class VideoDownloadDialog(QtWidgets.QDialog):
    def __init__(self, view):
        super().__init__(view.window())
        self.view = view
        self.setWindowTitle('网站视频下载')
        self.resize(720, 480)
        self.service = service_for(view)
        self.rows = {}
        layout = QtWidgets.QVBoxLayout(self)
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText('粘贴单个视频链接（网站支持和登录要求由 yt-dlp 决定）')
        layout.addWidget(self.url)
        form = QtWidgets.QFormLayout()
        self.directory = QtWidgets.QLineEdit(str(Path.home()/'Downloads/Prism'))
        form.addRow('保存目录', self.directory)
        self.cookie = QtWidgets.QLineEdit()
        self.cookie.setPlaceholderText('可选：自己导出的 Netscape cookies.txt 路径')
        form.addRow('登录 Cookie', self.cookie)
        self.browser = QtWidgets.QComboBox()
        self.browser.addItem('不读取浏览器登录信息', None)
        for name in ('Chrome', 'Edge', 'Firefox'):
            self.browser.addItem('允许本次任务使用 '+name+' 登录状态', name.lower())
        self.browser.setToolTip('只有主动选择后才由 yt-dlp 读取该浏览器 Cookie；不写入日志。')
        form.addRow('浏览器登录', self.browser)
        self.format = QtWidgets.QComboBox()
        for label, value in [('最佳画质', 'bv*+ba/b'), ('最高 1080p', 'bv*[height<=1080]+ba/b[height<=1080]'), ('最高 720p', 'bv*[height<=720]+ba/b[height<=720]')]:
            self.format.addItem(label, value)
        form.addRow('画质', self.format)
        self.format.clear()
        self.format.addItem('原画质（最高可用，不压缩）', 'bv*+ba/b')
        layout.addLayout(form)
        buttons = QtWidgets.QHBoxLayout()
        for label, command in [('解析链接', 'inspect'), ('加入下载', 'download')]:
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda checked=False, c=command: self.submit(c))
            if command == 'inspect':
                self.inspect_button = button
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.list = QtWidgets.QListWidget()
        layout.addWidget(self.list)
        self.status = QtWidgets.QLabel('最多同时下载 2 项；不会自动读取浏览器登录信息。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        bottom = QtWidgets.QHBoxLayout()
        for label, callback in [('取消所选任务', self.cancel), ('视频送入画布', self.import_video), ('提取帧到画布', self.extract)]:
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            bottom.addWidget(button)
        layout.addLayout(bottom)
        self.service.progress.connect(self.progress)
        self.service.finished.connect(self.finished)
        self.service.failed.connect(self.failed)
        self.url.setPlaceholderText('粘贴视频链接或分享文字；自动选择对应网站的下载工具')
        self.url.textChanged.connect(self.update_platform)
        self.update_platform()

    def update_platform(self):
        from prism.video_urls import normalize_share_url, platform_for_url
        try:
            platform = platform_for_url(normalize_share_url(self.url.text()))
        except ValueError:
            platform = 'ytdlp'
        self.inspect_button.setEnabled(platform == 'ytdlp')
        self.format.setEnabled(platform in ('ytdlp', 'bilibili'))
        self.cookie.setEnabled(platform in ('ytdlp', 'xiaohongshu', 'bilibili'))
        self.browser.setEnabled(platform == 'ytdlp')
        self.format.clear()
        self.format.addItem('原分辨率（不自动降清晰度）' if platform == 'bilibili' else '最高可用画质（不压缩）', 'bv*+ba/b')
        if platform == 'bilibili':
            self.format.addItem('当前会话最高可用（可能低于原分辨率）', 'best_available')
        if platform != 'ytdlp':
            self.status.setText('使用平台专用开源工具，以匿名会话下载；画质受平台可用权限限制。最多同时下载 2 项。')
        else:
            self.status.setText('其他网站由 yt-dlp 处理；最多同时下载 2 项，不自动读取浏览器登录信息。')

    def submit(self, command):
        from prism.actions.capture_workflow import target
        url = self.url.text().strip()
        context = target(self.view) if command == 'download' else None
        if command == 'download' and context is None:
            return
        try:
            identifier = self.service.submit(url, self.directory.text(), command,
                self.format.currentData(), self.cookie.text().strip() or None, context,
                cookie_browser=self.browser.currentData() if self.browser.isEnabled() else None,
                quality_policy='best_available' if self.format.currentData() == 'best_available' else 'original')
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        row = QtWidgets.QListWidgetItem('等待处理…')
        row.setData(QtCore.Qt.ItemDataRole.UserRole, identifier)
        self.rows[identifier] = row
        self.list.addItem(row)
        while self.list.count() > 40:
            old = self.list.takeItem(0)
            self.rows.pop(old.data(QtCore.Qt.ItemDataRole.UserRole), None)

    def progress(self, identifier, data):
        phase = data.get('phase', data.get('event', '处理中'))
        text = {'starting': '正在启动', 'inspect': '正在解析', 'download': '正在下载', 'postprocess': '正在合并或处理'}.get(phase, phase)
        if phase == 'store':
            text = '正在保留视频原件'
        if phase == 'session':
            text = '正在连接视频平台'
        if data.get('total'):
            text += f" {100*data.get('downloaded', 0)/data['total']:.1f}%"
        if identifier in self.rows:
            self.rows[identifier].setText(text)

    def finished(self, identifier, data):
        if identifier not in self.rows:
            return
        row = self.rows[identifier]
        row.setData(QtCore.Qt.ItemDataRole.UserRole+1, data)
        row.setText(('下载完成：' if data['event'] == 'completed' else '解析完成：' if data['event'] == 'inspected' else '已取消')+data.get('title', ''))
        if data['event'] == 'inspected':
            self.status.setText(f"{data.get('platform', '')} · {data.get('duration') or '?'} 秒 · {len(data.get('formats', []))} 个视频格式")

    def failed(self, identifier, message):
        if identifier in self.rows:
            self.rows[identifier].setText('失败：'+message)
        self.status.setText(message)

    def selected_result(self):
        row = self.list.currentItem()
        result = row.data(QtCore.Qt.ItemDataRole.UserRole+1) if row else None
        if not result or result.get('event') != 'completed':
            self.status.setText('请先选中已下载完成的视频')
            return None
        return result

    def cancel(self):
        row = self.list.currentItem()
        if row:
            self.service.cancel(row.data(QtCore.Qt.ItemDataRole.UserRole))

    def destination(self, result):
        context = result['context']
        pages = self.view.scene.workspace_pages
        if context['project_id'] != self.view.scene.project_id or (context['canvas_id'] != 'default-canvas' and not any(p.get('id') == context['canvas_id'] for p in pages)):
            self.status.setText('原目标工程或画布已改变。视频仍保留在下载目录，请手动导入。')
            return False
        worker = getattr(self.view, 'worker', None)
        if worker is not None and worker.isRunning():
            self.status.setText('正在导入其他素材，请稍后重试')
            return False
        self.view.window().open_workspace_page('canvas', context['canvas_id'])
        return True

    def import_video(self):
        result = self.selected_result()
        if result and self.destination(result):
            assets = result.get('assets') or [result]
            self.view.do_insert_images([asset.get('original_path') or asset['path'] for asset in assets], force_video_embed=True)

    def extract(self):
        result = self.selected_result()
        if not result or not self.destination(result):
            return
        assets = result.get('assets') or [dict(result, asset_type='video')]
        video = next((asset for asset in assets if asset.get('asset_type') == 'video'), None)
        if video is None:
            self.status.setText('该图文只有图片，可以直接导入画布，无需提取视频帧。')
            return
        from prism.widgets.extract_frames import ExtractFramesDialog
        path = video.get('original_path') or video['path']
        item = SimpleNamespace(_video_url=QtCore.QUrl.fromLocalFile(path), filename=path)
        dialog = ExtractFramesDialog(self.view, item, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted and dialog.images:
            if self.destination(result):
                self.view.add_frames_to_canvas(dialog.images)

    def closeEvent(self, event):
        if self.service.running or self.service.queue:
            self.status.setText('任务继续在后台运行，再次从菜单打开可查看。')
        self.hide()
        event.accept()
