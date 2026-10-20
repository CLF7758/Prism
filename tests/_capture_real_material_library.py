"""Capture the user's already-saved acceptance library with the shipped theme."""
from pathlib import Path
from PyQt6 import QtGui, QtWidgets
from prism.fileio.sql import SQLiteIO


def test_capture_actual_library(main_window, qtbot):
    root=Path(__file__).resolve().parents[1]
    app=QtWidgets.QApplication.instance()
    from prism import constants
    from prism.__main__ import create_palette_from_dict
    QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QtGui.QFont('Microsoft YaHei',11))
    app.setPalette(create_palette_from_dict(constants.COLORS))
    assets=root/'prism/assets'
    qss=(assets/'theme.qss').read_text(encoding='utf-8')
    app.setStyleSheet(qss.replace('url(assets/icons/',f'url({assets.as_posix()}/icons/'))
    window=main_window
    window.resize(1600,1050)
    SQLiteIO(str(root/'artifacts/material-tests/真实素材验收.prism'),window.view.scene,readonly=True).read()
    window.view.category_panel._install_resource_service()
    window.show();window.open_material_library()
    page=window.material_library
    qtbot.waitUntil(lambda: page.materials.ready,timeout=20000)
    first = page.records.records[0]['id']
    page.records.configure(first, blend_mode='auto')
    page._selection_changed(first)
    page.grid_button.click()
    from prism.material_textures import render_signature
    qtbot.waitUntil(lambda: page.records.get(first)['preview'].get('signature') == render_signature(page.records.get(first)),timeout=45000)
    qtbot.waitUntil(lambda: not page._jobs,timeout=30000)
    qtbot.wait(1000)
    window.grab().save(str(root/'artifacts/material-tests/material-library-real-assets-final.png'))
