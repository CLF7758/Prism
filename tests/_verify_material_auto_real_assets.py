"""Native production-default startup, real stored originals and live inspector."""
from pathlib import Path
import hashlib
import json

from PyQt6 import QtGui,QtWidgets
from prism.fileio.sql import SQLiteIO
from prism.material_textures import render_signature


def test_real_assets_automatic_render_and_controls(main_window,qtbot):
    from prism import constants
    from prism.__main__ import create_palette_from_dict
    root=Path(__file__).resolve().parents[1]
    output=root/'artifacts/material-tests'
    source=output/'真实素材验收.prism'
    app=QtWidgets.QApplication.instance()
    assert app.platformName()=='windows'
    QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QtGui.QFont('Microsoft YaHei',11))
    app.setPalette(create_palette_from_dict(constants.COLORS))
    assets=root/'prism/assets'
    qss=(assets/'theme.qss').read_text(encoding='utf-8')
    app.setStyleSheet(qss.replace('url(assets/icons/',f'url({assets.as_posix()}/icons/'))
    window=main_window;window.resize(1600,1050)
    scene=window.view.scene
    SQLiteIO(str(source),scene,readonly=True).read()
    window.view.category_panel._install_resource_service()
    window.show();window.open_material_library()
    page=window.material_library
    records=list(page.records.records)
    hashes={f['attachmentId']:f['sha256'] for record in records for f in record.get('files',[])}
    report=[]
    for index,original in enumerate(records):
        page.records.configure(original['id'],blend_mode='auto')
        page._selection_changed(original['id'])
        def fresh():
            record=page.records.get(original['id'])
            return record['preview'].get('signature')==render_signature(record) and not record['preview'].get('stale',True)
        qtbot.waitUntil(fresh,timeout=60000)
        viewer=page.details.inline_render
        assert viewer.isVisible()
        assert page.details.auto_mode.text()==('自动：透明裁切' if index==3 else '自动：不透明')
        page.details.slots['roughness'].slider.setValue(600)
        qtbot.waitUntil(fresh,timeout=30000)
        for slot in ('roughness','basecolor'):
            if slot=='roughness':
                page.details.slots[slot].invert.setChecked(True)
            else:
                page.change_slot_options(slot,{'color':[.7,1.,.8]})
            qtbot.waitUntil(fresh,timeout=40000)
        page.details.flip_x.setChecked(True)
        qtbot.waitUntil(fresh,timeout=40000)
        qtbot.wait(200)
        viewer.web.grab().save(str(output/f'auto-material-{index+1}.png'))
        report.append(dict(name=original['name'],autoMode=page.details.auto_mode.text(),automaticUpdates=True))
    for attachment_id,sha in hashes.items():
        assert hashlib.sha256(scene.attachments[attachment_id][2]).hexdigest()==sha
    page._selection_changed(records[0]['id'])
    qtbot.waitUntil(lambda: not page._jobs,timeout=30000)
    qtbot.wait(1000)
    page.store_to_scene()
    SQLiteIO(str(output/'真实素材-自动更新.prism'),scene,create_new=True).write()
    window.grab().save(str(output/'material-library-compact-auto.png'))
    (output/'auto-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
