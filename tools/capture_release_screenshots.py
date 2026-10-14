"""Render the actual Prism UI using only reproducible demonstration content."""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--disable-gpu'
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.argv = [sys.argv[0]]
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.__main__ import PrismApplication, PrismMainWindow
from prism.config import PrismSettings
from prism.items import PrismPixmapItem
from prism.utils import create_palette_from_dict
from prism import constants
from prism.widgets.duplicate_finder import DuplicateFinderDialog


def pump(app, duration=1):
    end = time.monotonic()+duration
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(.01)


def illustration(index):
    colors = ['#648a8c', '#ac855d', '#7786a8', '#777f70', '#ab786e', '#59898d']
    image = QtGui.QImage(640, 420, QtGui.QImage.Format.Format_RGB32)
    p = QtGui.QPainter(image)
    gradient = QtGui.QLinearGradient(0, 0, 640, 420)
    gradient.setColorAt(0, QtGui.QColor(colors[index%6]).lighter(150))
    gradient.setColorAt(1, QtGui.QColor(colors[index%6]).darker(180))
    p.fillRect(image.rect(), gradient)
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor('#dcd3ab'))
    p.drawEllipse(430, 40+index%3*15, 65, 65)
    for n in range(3):
        p.setBrush(QtGui.QColor(colors[index%6]).darker(110+n*25))
        path = QtGui.QPainterPath(QtCore.QPointF(0, 420))
        path.lineTo(0, 230+n*45)
        for x in range(0, 700, 80):
            path.lineTo(x, 180+n*50+((x//80+index+n)%3)*38)
        path.lineTo(640, 420)
        p.drawPath(path)
    p.setPen(QtGui.QColor('#eeeeee'))
    p.setFont(QtGui.QFont('Segoe UI', 17))
    p.drawText(24, 370, ['LANDSCAPE', 'STRUCTURE', 'LIGHT STUDY'][index%3])
    p.setFont(QtGui.QFont('Segoe UI', 10))
    p.drawText(25, 397, f'PRISM / ORIGINAL DEMO / {index+1:02d}')
    p.end()
    return image


def main():
    settings = tempfile.TemporaryDirectory(prefix='prism-release-preview-')
    PrismSettings.get_settings_dir = staticmethod(lambda: settings.name)
    app = PrismApplication(['prism-release-preview'])
    for path in list(Path('C:/Windows/Fonts').glob('msyh*.ttc')) + [Path('C:/Windows/Fonts/segoeui.ttf')]:
        QtGui.QFontDatabase.addApplicationFont(str(path))
    app.setFont(QtGui.QFont('Microsoft YaHei', 10))
    app.setPalette(create_palette_from_dict(constants.COLORS))
    qss = (root/'prism/assets/theme.qss').read_text(encoding='utf-8')
    app.setStyleSheet(qss.replace('url(assets/icons/', 'url('+(root/'prism/assets/icons').as_posix()+'/'))
    w = PrismMainWindow(app)
    w.resize(1440, 900)
    canvas = w.workspace_service.add('canvas', '光影与空间')
    doc = w.workspace_service.add('document', '创作简报')
    doc['content'] = '<h1>光影与空间</h1><p>将灵感、参考与创作计划放在同一个本地项目。</p><h2>视觉方向</h2><ul><li>冷暖关系：青绿与沙金</li><li>构图：大形体、清晰的空间层次</li><li>光照：低角度日光与柔和雾气</li></ul><h2>下一步</h2><p>整理素材 → 制作草图 → 建立场景 → 复盘迭代</p>'
    mind = w.workspace_service.add('mindmap', '制作计划')
    mind['content'] = {'data': {'text': '场景创作'}, 'children': [
        {'data': {'text': '视觉研究'}, 'children': [{'data': {'text': '光照'}}, {'data': {'text': '色彩'}}]},
        {'data': {'text': '场景制作'}, 'children': [{'data': {'text': '构图草案'}}, {'data': {'text': '模型与材质'}}]},
        {'data': {'text': '复盘交付'}}]}
    items = []
    for index in range(18):
        item = PrismPixmapItem(illustration(index))
        item._title = ['山形与轮廓', '空间层次', '晨光研究'][index%3]+f' {index+1:02d}'
        item._canvas_id = canvas['id']
        item.tags = ['光影参考', '场景设计']
        item._rating = 4 if index%3 == 0 else 0
        item.setPos(index%6*655, index//6*435)
        w.view.scene.addItem(item)
        items.append(item)
    w.view.scene.tag_names = ['光影参考', '场景设计']
    w.view.category_panel.rebuild_workspace_tree()
    w.view.category_panel.refresh_tags()
    w.show()
    w.open_workspace_page('canvas', canvas['id'])
    pump(app, 2)
    w.view.on_action_fit_scene()
    items[4].setSelected(True)
    pump(app, 2)
    out = root/'docs/images'
    out.mkdir(exist_ok=True)
    def shot(name, widget=w):
        pump(app, 1)
        assert widget.grab().save(str(out/name))
        print(name, flush=True)
    shot('release-canvas.png')
    w.asset_browser.open('all')
    pump(app, 2)
    shot('release-library.png')
    w.open_workspace_page('document', doc['id'])
    pump(app, 3)
    shot('release-document.png')
    w.open_workspace_page('mindmap', mind['id'])
    pump(app, 3)
    shot('release-mindmap.png')
    w.open_workspace_page('canvas', canvas['id'])
    dialog = DuplicateFinderDialog(w.view, [[items[0], items[6]], [items[1], items[7]]])
    dialog.show()
    shot('release-duplicates.png', dialog)
    dialog.close()
    w.view.undo_stack.setClean()
    w.browser_capture.stop()
    os._exit(0)


if __name__ == '__main__':
    main()
