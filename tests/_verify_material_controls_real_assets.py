import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch
from PyQt6 import QtGui,QtWidgets
from prism.fileio.sql import SQLiteIO
from prism.material_textures import render_signature


def test_real_material_controls(main_window,qtbot,tmp_path):
    from prism import constants
    from prism.__main__ import create_palette_from_dict
    from tests.test_material_view_controls import js
    root=Path(__file__).resolve().parents[1];out=root/'artifacts/material-tests'
    app=QtWidgets.QApplication.instance();assert app.platformName()=='windows'
    app.setFont(QtGui.QFont('Microsoft YaHei',11));app.setPalette(create_palette_from_dict(constants.COLORS))
    assets=root/'prism/assets';qss=(assets/'theme.qss').read_text(encoding='utf-8')
    app.setStyleSheet(qss.replace('url(assets/icons/',f'url({assets.as_posix()}/icons/'))
    window=main_window;window.resize(1650,1100)
    SQLiteIO(str(out/'真实素材验收.prism'),window.view.scene,readonly=True).read()
    window.view.category_panel._install_resource_service()
    window.show();window.open_material_library();page=window.material_library
    records=page.records.list_records();report=[]
    for index,record in enumerate(records):
        page.records.configure(record['id'],blend_mode='auto')
        page._selection_changed(record['id'])
        def fresh():
            current=page.records.get(record['id'])
            return current['preview'].get('signature')==render_signature(current) and not current['preview'].get('stale',True)
        qtbot.waitUntil(fresh,timeout=60000)
        slider=page.details.slots['roughness'].slider
        with patch.object(page.materials,'set_records',wraps=page.materials.set_records) as grid_refresh:
            start=time.perf_counter()
            for value in range(100,901,20):slider.setValue(value)
            elapsed=time.perf_counter()-start
            assert grid_refresh.call_count==0
            assert elapsed<2,(record['name'],elapsed)
        qtbot.waitUntil(fresh,timeout=30000)
        page.open_preview()
        qtbot.waitUntil(lambda:bool(page._dialogs),timeout=20000)
        dialog=page._dialogs[-1];viewer=dialog.material_viewer
        qtbot.waitUntil(lambda:viewer.ready,timeout=15000)
        qtbot.waitUntil(lambda:js(viewer,'!!sphere&&!!material',qtbot),timeout=30000)
        viewer.view_channel('roughness')
        qtbot.waitUntil(lambda:js(viewer,'!!debugMaterial && debugMaterial.isReady(sphere)',qtbot),timeout=20000)
        qtbot.wait(300)
        viewer.web.grab().save(str(out/f'real-roughness-{index+1}.png'))
        for shape in ('sphere','cylinder','box','plane'):
            viewer.set_shape(shape);qtbot.wait(100)
            assert js(viewer,'previewShape',qtbot)==shape
        dialog.close()
        for file in record['files']:
            assert hashlib.sha256(window.view.scene.attachments[file['attachmentId']][2]).hexdigest()==file['sha256']
        report.append(dict(name=record['name'],parameterEventMilliseconds=elapsed/41*1000,rawFilesUnchanged=True))
    page._selection_changed(records[0]['id']);page.grid_button.click()
    qtbot.waitUntil(lambda:not page._jobs,timeout=40000);qtbot.wait(700)
    window.grab().save(str(out/'material-library-controls-final.png'))
    (out/'controls-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
