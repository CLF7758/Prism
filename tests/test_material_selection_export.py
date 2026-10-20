import hashlib
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from PyQt6 import QtCore,QtWidgets
from prism.material_textures import prepare_texture
from tests.test_material_cards import javascript


def test_ctrl_selection_and_context_export(main_window,qtbot,tmp_path):
    window=main_window;window.resize(1400,900);window.show();window.open_material_library()
    page=window.material_library
    records=[]
    for i in range(3):
        record=page.records.create(name='材质'+str(i))
        file=tmp_path/(str(i)+'.png');Image.new('RGB',(8,8),(i*50,20,40)).save(file)
        page.records.configure(record['id'],slot='basecolor',prepared=prepare_texture(file))
        records.append(record)
    qtbot.waitUntil(lambda: page.materials.ready,timeout=15000)
    qtbot.waitUntil(lambda: javascript(page.materials,"document.querySelectorAll('.asset-card').length",qtbot)==3)
    javascript(page.materials,"document.querySelectorAll('.asset-card')[0].click()",qtbot)
    qtbot.waitUntil(lambda:page.selected_id==records[0]['id'])
    javascript(page.materials,"document.querySelectorAll('.asset-card')[1].dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}))",qtbot)
    qtbot.waitUntil(lambda:len(page.materials.selected_ids)==2)
    assert page.materials.selected_ids=={r['id'] for r in records[:2]}
    qtbot.wait(200)
    assert javascript(page.materials,"document.querySelectorAll('.asset-card.selected').length",qtbot)==2
    with patch.object(QtWidgets.QFileDialog,'getExistingDirectory',return_value=str(tmp_path/'out')):
        javascript(page.materials,"document.querySelectorAll('.asset-card')[0].dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:40,clientY:40}))",qtbot)
        assert '2' in javascript(page.materials,"document.getElementById('material-menu').textContent",qtbot)
        javascript(page.materials,"document.querySelector('#material-menu button').click()",qtbot)
        qtbot.waitUntil(lambda:len(list((tmp_path/'out').rglob('*.png')))==2,timeout=15000)
    for exported in (tmp_path/'out').rglob('*.png'):
        source=tmp_path/exported.name
        assert exported.read_bytes()==source.read_bytes()
    assert page.details.export_button.isVisible()
    assert page.details.tag_edit.isVisible() and page.details.notes.isVisible()
    assert page.back_button.isHidden() and page.title.isHidden()
    assert not page.details.flip_x.isVisible()
    assert not page.details.slots['normal'].flip_x.isVisible()
    assert page.details.slots['normal'].invert.isVisible()


def test_switch_reuses_web_surfaces(main_window,qtbot):
    window=main_window;window.show();window.open_material_library()
    page=window.material_library;page.create_material()
    grid=page.materials.web;renderer=page.details.inline_render.web
    for _ in range(3):
        window.tabs.setCurrentIndex(0)
        window.open_material_library()
    assert page.materials.web is grid
    assert page.details.inline_render.web is renderer
