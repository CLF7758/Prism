"""Render a reproducible sidebar preview using synthetic, temporary data."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.__main__ import PrismApplication, PrismMainWindow
from prism.config import PrismSettings
from prism.items import PrismPixmapItem
from tools.preview_native_ui import pump


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / 'output' / 'ui-screenshots'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='prism-sidebar-') as settings, patch.object(
            PrismSettings, 'get_settings_dir', return_value=settings), patch.object(
            QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        app = PrismApplication(['sidebar-preview'])
        theme = (root / 'prism/assets/theme.qss').read_text(encoding='utf-8')
        theme = theme.replace('url(assets/icons/', 'url(' + (root / 'prism/assets/icons').as_posix() + '/')
        app.setStyleSheet(theme)
        window = PrismMainWindow(app)
        window.resize(1440, 960)
        page = window.workspace_service.add('canvas', '灵感采集')
        library = window.view.category_panel
        folder = library.resource_service.createFolder('canvas', '设计素材')
        library.resource_service.createPage('canvas', '角色设计', page['id'], folder['id'])
        library.rebuild_workspace_tree()
        library.resource_tree.expandAll()
        window.view.scene.tag_names = ['灵感', '角色设计', '色彩研究']
        library.refresh_tags()
        selected = None
        for index in range(6):
            image = QtGui.QImage(420, 300, QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor('#273E59'))
            painter = QtGui.QPainter(image)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor('#E5C7AA'))
            painter.drawEllipse(125, 65, 170, 180)
            painter.setBrush(QtGui.QColor('#D9959B'))
            painter.drawEllipse(145, 95, 35, 30)
            painter.drawEllipse(245, 95, 35, 30)
            painter.setBrush(QtGui.QColor('#92B5C1'))
            painter.drawRoundedRect(60, 245, 300, 40, 18, 18)
            painter.setBrush(QtGui.QColor('#FFFFFF'))
            for x, y in ((50, 40), (330, 55), (70, 155), (350, 190)):
                painter.drawEllipse(x, y, 4, 4)
            painter.end()
            item = PrismPixmapItem(image)
            item.filename = f'角色参考 {index + 1}.jpg'
            item._title = '星夜 · 角色与色彩' if index == 0 else item.filename
            item._notes = '收集柔和的色彩与圆润的轮廓。\n\n深蓝背景与暖色角色形成层次，用于角色设计和氛围参考。' if index == 0 else ''
            item._tags = ['灵感', '角色设计'] if index == 0 else []
            item._rating = 4 if index == 0 else 0
            item._canvas_id = page['id']
            item.setPos((index % 3) * 480, (index // 3) * 340)
            window.view.scene.addItem(item)
            if index == 0:
                selected = item
        window.show()
        window.open_workspace_page('canvas', page['id'])
        selected.setSelected(True)
        pump(app)
        window.view.on_action_fit_scene()
        pump(app)
        window.grab().save(str(output / 'eagle-sidebars.png'))
        window.canvas_toolbar.width_combo.showPopup()
        pump(app)
        window.canvas_toolbar.width_combo.view().window().grab().save(str(output / 'canvas-width-popup.png'))
        window.canvas_toolbar.width_combo.hidePopup()
        window.canvas_toolbar.style_combo.showPopup()
        pump(app)
        window.canvas_toolbar.style_combo.view().window().grab().save(str(output / 'canvas-style-popup.png'))
        window.canvas_toolbar.style_combo.hidePopup()
        window.asset_browser.open('all')
        pump(app)
        window.grab().save(str(output / 'eagle-library.png'))
        window.asset_browser.list.setCurrentIndex(window.asset_browser.model.index(0))
        pump(app)
        window.grab().save(str(output / 'eagle-library-selected.png'))
        window._toggle_sidebar('right')
        pump(app)
        window.grab().save(str(output / 'eagle-right-collapsed.png'))
        window._toggle_sidebar('right')
        window._toggle_sidebars()
        pump(app)
        window.grab().save(str(output / 'eagle-both-collapsed.png'))
        window._toggle_sidebars()
        pump(app)
        window.grab().save(str(output / 'eagle-restored.png'))
        window.resize(900, 600)
        pump(app)
        window.grab().save(str(output / 'eagle-sidebars-narrow.png'))
        window.view.scene.undo_stack.setClean()
        window.close()
        pump(app, .2)
        print(output)


if __name__ == '__main__':
    main()
