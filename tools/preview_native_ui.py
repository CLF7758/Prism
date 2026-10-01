"""Render synthetic preview data without opening or saving a user project."""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.argv = [sys.argv[0]]
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.__main__ import PrismApplication, PrismMainWindow
from prism.items import PrismPixmapItem
from prism.config import PrismSettings


def pump(app, seconds=1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(.01)


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / 'output' / 'ui-screenshots'
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='prism-ui-preview-') as settings_dir, \
            patch.object(PrismSettings, 'get_settings_dir', return_value=settings_dir), \
            patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
        app = PrismApplication(['prism-ui-preview'])
        qss = (root / 'prism/assets/theme.qss').read_text(encoding='utf-8')
        qss = qss.replace('url(assets/icons/', 'url(' + (root / 'prism/assets/icons').as_posix() + '/')
        app.setStyleSheet(qss)
        window = PrismMainWindow(app)
        window.resize(1440, 900)
        canvas = window.workspace_service.add('canvas', '灵感收集')
        document = window.workspace_service.add('document', '项目说明')
        mind = window.workspace_service.add('mindmap', '产品规划')
        document['content'] = '<h1>项目视觉方向</h1><p>图像探索、文字推演与结构整理。</p><h2>构图原则</h2><ul><li>先建立明暗关系，再处理材质。</li><li>保持统一的自然光方向。</li></ul>'
        mind['content'] = {'data': {'text': '产品规划'}, 'children': [
            {'data': {'text': '目标用户'}, 'children': [{'data': {'text': '独立创作者'}}, {'data': {'text': '小型工作室'}}]},
            {'data': {'text': '功能范围'}, 'children': [{'data': {'text': '画布'}}, {'data': {'text': '脑图'}}, {'data': {'text': '文档'}}]}
        ]}
        window.view.category_panel.rebuild_workspace_tree()
        window.view.scene.tag_names = ['风格/古风', '未分配标签']
        window.view.category_panel.refresh_tags()
        for index, colour in enumerate(['#8a6a4e', '#6e8590', '#c08b54', '#7fa38f', '#a06a72', '#5c7d93']):
            pixmap = QtGui.QPixmap(420, 280)
            pixmap.fill(QtGui.QColor(colour))
            item = PrismPixmapItem(pixmap.toImage())
            item.filename = f'参考素材 {index + 1}.jpg'
            item._canvas_id = canvas['id']
            item.setPos((index % 3) * 480, (index // 3) * 340)
            window.view.scene.addItem(item)
        window.show()
        window.open_workspace_page('canvas', canvas['id'])
        first = next(iter(window.view.scene.items_for_save()))
        first.setSelected(True)
        pump(app)
        window.view.on_action_fit_scene()
        pump(app)
        window.grab().save(str(out / 'canvas.png'))
        window.resize(900, 600)
        pump(app)
        window.canvas_toolbar.filter_button.click()
        pump(app)
        window.grab().save(str(out / 'canvas-narrow-filter.png'))
        window.resize(1440, 900)
        panel = window._page_panels[document['id']]
        window.open_workspace_page('document', document['id'])
        pump(app, 3)
        window.grab().save(str(out / 'document.png'))
        window.open_workspace_page('mindmap', mind['id'])
        pump(app, 3)
        window.grab().save(str(out / 'mindmap.png'))
        library = window.view.category_panel
        library.search.setText('工作室')
        pump(app)
        library._refresh_project_search()
        for i in range(library.search_results.topLevelItemCount()):
            group = library.search_results.topLevelItem(i)
            if group.text(0) == '脑图节点':
                library._activate_search_result(group.child(0))
                break
        pump(app, 3)
        window.grab().save(str(out / 'search-mindmap.png'))
        library.search.clear()
        library.search.setText('自然光')
        pump(app)
        library._refresh_project_search()
        for i in range(library.search_results.topLevelItemCount()):
            group = library.search_results.topLevelItem(i)
            if group.text(0) == '文档正文':
                library._activate_search_result(group.child(0))
                break
        pump(app, 3)
        window.grab().save(str(out / 'search-document.png'))
        library.search.clear()
        from types import SimpleNamespace
        from prism.widgets.extract_frames import ExtractFramesDialog
        dialog = ExtractFramesDialog(window.view, SimpleNamespace(filename='sample.ts', _video_url=None))
        dialog.show()
        pump(app)
        dialog.grab().save(str(out / 'extract-frames.png'))
        dialog.close()
        window.view.scene.undo_stack.setClean()
        window.close()
        pump(app)
        print(out)


if __name__ == '__main__':
    main()
