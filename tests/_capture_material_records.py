from pathlib import Path
from PyQt6 import QtGui, QtWidgets


def test_capture(main_window, qtbot):
    window = main_window
    app = QtWidgets.QApplication.instance()
    from prism import constants
    from prism.__main__ import create_palette_from_dict
    QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QtGui.QFont('Microsoft YaHei', 12))
    window.setFont(app.font())
    app.setPalette(create_palette_from_dict(constants.COLORS))
    assets = Path('prism/assets').resolve()
    qss = (assets / 'theme.qss').read_text(encoding='utf-8')
    app.setStyleSheet(qss.replace('url(assets/icons/', f'url({assets.as_posix()}/icons/'))
    window.resize(1440, 900)
    window.show()
    panel = window.view.category_panel
    folder = panel.resource_service.createFolder('material', '木头')
    panel.resource_service.createFolder('material', '金属')
    window.open_material_library(folder['id'])
    page = window.material_library
    page.create_material()
    page.details.name.setText('原木 · 浅色橡木')
    page.details.notes.setPlainText('用于室内家具和木饰面。\n这一步先保存资料，后续再收进颜色、法线和粗糙度贴图。')
    page.flush_details()
    page.records.create(folder['id'], '胡桃木')
    page.records.create(folder['id'], '风化木板')
    tree = panel.resource_tree
    tree.expand(tree.model().index(3, 0))
    qtbot.waitUntil(lambda: page.materials.ready, timeout=15000)
    from tests.test_material_cards import javascript
    qtbot.waitUntil(lambda: javascript(page.materials,
        "document.querySelectorAll('.asset-card').length", qtbot) == 3, timeout=10000)
    qtbot.wait(200)
    target = Path('docs/development/screenshots/material-library-surface-cards.png')
    assert window.grab().save(str(target))
