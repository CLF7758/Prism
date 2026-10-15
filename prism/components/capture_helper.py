"""Standalone Qt5 bridge to ScreenPinKit. Never import this in the Qt6 process."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import traceback
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--session-dir', required=True)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--persistent', action='store_true')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    directory = Path(args.session_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    output = sys.stdout
    sys.stdout = sys.stderr  # Upstream prints must never corrupt the protocol.
    sys.path.insert(0, str(source))
    from PyQt5 import QtCore, QtGui, QtWidgets
    from manager.pin_window_manager import PinWindowManager
    from view.screen_shot_window import ScreenShotWindow
    from plugin import pluginMgr, GlobalEventEnum
    from common import cfg

    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    cache_directory = directory/'upstream-cache'
    cache_directory.mkdir(exist_ok=True)
    cfg.cacheFolder.value = str(cache_directory)
    # Prism exposes manual rectangle selection, not automatic window guessing.
    # Avoid a second full-screen capture + OpenCV contour scan before showing.
    cfg.isAutoFindWindow.value = False

    def send(event, **fields):
        output.write(json.dumps(dict(event=event, **fields), ensure_ascii=False)+'\n')
        output.flush()

    class ManualCapture(ScreenShotWindow):
        def initFindRectManager(self):
            self.findRectMgr = None

        def paintEvent(self, event):
            super().paintEvent(event)
            if not getattr(self, '_ready_reported', False):
                self._ready_reported = True
                send('overlay_ready', request_id=bridge.request)

    class Bridge(QtCore.QObject):
        incoming = QtCore.pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self.manager = PinWindowManager()
            self.capture = None
            self.bar = None
            self.request = None
            self.destination = 'pin'
            self.incoming.connect(self.dispatch)
            self.idle = QtCore.QTimer(self)
            self.idle.setInterval(60000)
            self.idle.timeout.connect(self.expire)
            self.idle.start()

        def expire(self):
            if not args.persistent and not self.capture and not self.manager._windowsDict:
                app.quit()

        def save(self, pixmap):
            path = directory/(uuid.uuid4().hex+'.png')
            if not pixmap.save(str(path), 'PNG'):
                raise OSError('Cannot write screenshot')
            return str(path)

        def export_pin(self, window):
            try:
                send('import_pin', path=self.save(window.grabImage(False)))
            except Exception as error:
                send('failed', error=str(error))

        def choose(self, destination):
            self.destination = destination
            if self.bar:
                self.bar.hide()
            if self.capture:
                self.capture.snip()

        def selection(self, _rect):
            if self.bar:
                self.bar.deleteLater()
            self.bar = QtWidgets.QWidget(None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
            self.bar.setStyleSheet('QWidget{background:#292b30;color:#eee;} QPushButton{padding:8px;border:1px solid #444;border-radius:4px;} QPushButton:hover{background:#425475;}')
            layout = QtWidgets.QHBoxLayout(self.bar)
            for title, mode in (('钉在桌面', 'pin'), ('送入画布', 'canvas'), ('两者都要', 'both')):
                button = QtWidgets.QPushButton(title)
                button.clicked.connect(lambda checked=False, mode=mode: self.choose(mode))
                layout.addWidget(button)
            cancel = QtWidgets.QPushButton('取消')
            cancel.clicked.connect(self.capture.cancelScreenShot)
            layout.addWidget(cancel)
            self.bar.adjustSize()
            screen = app.screenAt(QtGui.QCursor.pos()) or app.primaryScreen()
            area = screen.availableGeometry()
            point = QtGui.QCursor.pos()+QtCore.QPoint(12, 16)
            point.setX(min(max(area.left(), point.x()), area.right()-self.bar.width()))
            point.setY(min(max(area.top(), point.y()), area.bottom()-self.bar.height()))
            self.bar.move(point)
            self.bar.show()

        def snipped(self, rect, pixmap):
            if pixmap.isNull():
                return
            request, self.request = self.request, None
            try:
                # Desktop references use Prism's native Qt6 drawing controls;
                # ScreenPinKit remains the screenshot/selection engine.
                path = self.save(pixmap)
                send('captured', request_id=request, destination=self.destination, path=path,
                     width=pixmap.width(), height=pixmap.height())
            except Exception as error:
                traceback.print_exc(file=sys.stderr)
                send('failed', request_id=request, error=str(error))

        def closed(self):
            if self.bar:
                self.bar.deleteLater()
                self.bar = None
            capture, self.capture = self.capture, None
            if capture:
                capture.deleteLater()
            if self.request:
                send('cancelled', request_id=self.request)
                self.request = None

        def pin(self, point, size, pixmap):
            if len(self.manager._windowsDict) >= 20:
                raise ValueError('最多同时打开 20 个贴图窗口')
            pixels = sum(window.painterWidget.physicalPixmap.width()*window.painterWidget.physicalPixmap.height()
                         for window in self.manager._windowsDict.values())
            if (pixels+pixmap.width()*pixmap.height())*4 > 128*1024*1024:
                raise ValueError('贴图显示缓存已达 128 MiB，请关闭部分参考图')
            self.manager.addPinWindow(point, size, pixmap)

        def dispatch(self, message):
            request = message.get('request_id')
            try:
                command = message['command']
                if command == 'capture':
                    if self.capture:
                        raise ValueError('已有截图正在进行')
                    self.request = request
                    self.destination = 'pin'
                    if args.self_test:
                        from canvas_item.canvas_util import CanvasUtil
                        pixels = QtGui.QPixmap(320, 180)
                        pixels.fill(QtGui.QColor(60, 100, 140))
                        CanvasUtil.grabScreens = staticmethod(lambda: (pixels, QtCore.QRect(0, 0, 320, 180)))
                    self.capture = ManualCapture()
                    self.capture.snipedSignal.connect(self.snipped)
                    self.capture.closedSignal.connect(self.closed)
                    self.capture.selectionReady.connect(self.selection)
                    self.capture.reShow()
                    if args.self_test:
                        self.capture._pt_start = QtCore.QPointF(10, 20)
                        self.capture._pt_end = QtCore.QPointF(150, 100)
                        self.capture.hasScreenShot = True
                        self.destination = 'canvas'
                        self.capture.snip()
                elif command == 'cancel':
                    if self.capture:
                        self.capture.cancelScreenShot()
                elif command == 'pin_file':
                    path = Path(message['path']).resolve()
                    if not path.is_relative_to(directory):
                        raise ValueError('Invalid image path')
                    pixmap = QtGui.QPixmap(str(path))
                    if pixmap.isNull():
                        raise ValueError('无法读取贴图文件')
                    screen = app.screenAt(QtGui.QCursor.pos()) or app.primaryScreen()
                    limit = screen.availableGeometry().size()*.8
                    size = pixmap.size().scaled(limit, QtCore.Qt.KeepAspectRatio)
                    self.pin(QtGui.QCursor.pos()-QtCore.QPoint(size.width()//2, size.height()//2), size, pixmap)
                    send('pinned', request_id=request)
                elif command in ('hide_all', 'show_all'):
                    for window in self.manager._windowsDict.values():
                        window.setVisible(command == 'show_all')
                elif command == 'shutdown':
                    app.quit()
                elif command == 'ack':
                    path = Path(message['path']).resolve()
                    if path.is_relative_to(directory):
                        path.unlink(missing_ok=True)
                else:
                    raise ValueError('Unknown command')
            except Exception as error:
                traceback.print_exc(file=sys.stderr)
                send('failed', request_id=request, error=str(error))

    bridge = Bridge()
    original = pluginMgr.handleEvent

    def event(event_name, *args, **kwargs):
        original(event_name, *args, **kwargs)
        if event_name == GlobalEventEnum.RegisterContextMenuEvent:
            parent = kwargs.get('parent')
            action = QtWidgets.QAction('送入 Prism 当前画布', parent)
            action.triggered.connect(lambda: bridge.export_pin(parent))
            kwargs['actions'].append(action)
    pluginMgr.handleEvent = event

    def receive():
        for line in sys.stdin:
            try:
                if len(line) > 65536:
                    break
                bridge.incoming.emit(json.loads(line))
            except (ValueError, TypeError):
                continue
        bridge.incoming.emit(dict(command='shutdown'))
    threading.Thread(target=receive, daemon=True).start()
    send('ready', protocol_version=1)
    app.exec()


if __name__ == '__main__':
    main()
