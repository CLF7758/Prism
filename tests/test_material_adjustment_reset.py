import hashlib
from pathlib import Path
from PIL import Image
from prism.material_parameters import slot_options
from prism.material_textures import prepare_texture, render_signature


def test_selecting_a_saved_material_also_discards_old_numeric_adjustments(main_window, tmp_path):
    main_window.open_material_library()
    page = main_window.material_library
    record = page.records.create(name='Previously adjusted')
    source = tmp_path / 'normal.png'
    Image.new('RGB', (8,8), (128,128,255)).save(source)
    page.records.configure(record['id'], slot='normal', prepared=prepare_texture(source), channel='RGB')
    page.records.configure(record['id'], slot_settings=('normal', {'value': 2.75}))
    before = page.records.get(record['id'])
    page._selection_changed(record['id'])
    page._auto_timer.stop()
    after = page.records.get(record['id'])
    assert after['textures'] == before['textures']
    assert page.details.slots['normal'].amount.value() == 1
    assert page.details.slots['normal'].slider.value() == page.details.slots['normal'].slider.maximum()


def test_switch_discards_adjustments_but_keeps_sources_and_channels(main_window, qtbot, tmp_path):
    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library
    a = page.records.create(name='A')
    b = page.records.create(name='B')
    source = tmp_path/'rough.png'
    Image.new('RGB', (16,16), (30,120,200)).save(source)
    page.records.configure(a['id'], slot='roughness', prepared=prepare_texture(source), channel='G')
    page.records.configure(b['id'], slot='roughness', prepared=prepare_texture(source), channel='R')
    page._selection_changed(a['id'])
    page._auto_timer.stop()
    before = page.records.get(a['id'])
    # An already-rendered default card must be restored, not the discarded tint.
    thumbnail = tmp_path/'thumbnail.png'
    Image.new('RGB', (256,256), 'black').save(thumbnail)
    assert page.records.save_preview(a['id'], render_signature(before), thumbnail.read_bytes())
    page.change_slot_options('roughness', {'factor':.2, 'invert':True})
    page.change_slot_options('basecolor', {'color':[1,.1,.1], 'value':.4})
    page._selection_changed(b['id'])
    page._auto_timer.stop()
    reset = page.records.get(a['id'])
    assert 'slots' not in reset['preview']['properties']
    assert reset['textures'] == before['textures'] and reset['files'] == before['files']
    assert reset['textures']['roughness']['channel'] == 'G'
    assert page.details.slots['roughness'].amount.value() == 1
    assert page.details.slots['basecolor'].amount.value() == 1
    assert page.records.scene.attachments[reset['preview']['thumbnailAttachmentId']][2] == thumbnail.read_bytes()
    assert reset['preview']['signature'] == render_signature(reset)
    page._selection_changed(a['id'])
    page._auto_timer.stop()
    assert page.details.slots['roughness'].amount.value() == 1
    assert slot_options(reset['preview']['properties'], 'basecolor')['color'] == [1.,1.,1.]
    for file in reset['files']:
        assert hashlib.sha256(page.records.scene.attachments[file['attachmentId']][2]).hexdigest() == file['sha256']


def test_save_flush_discards_adjustments(main_window, qtbot):
    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library
    page.create_material()
    page.change_slot_options('metallic', {'value':.9})
    page.store_to_scene()
    page._auto_timer.stop()
    material = page.records.get(page.selected_id)
    assert 'slots' not in material['preview']['properties']
    assert page.details.slots['metallic'].amount.value() == 0


def test_create_and_search_leave_also_reset_previous_material(main_window):
    main_window.open_material_library()
    page = main_window.material_library
    page.create_material()
    first = page.selected_id
    page.change_slot_options('roughness', {'value':.1})
    page.create_material()
    page._auto_timer.stop()
    assert not page.records.get(first)['preview']['properties'].get('slots')
    second = page.selected_id
    page.change_slot_options('roughness', {'value':.2})
    page.search.setText('this-name-does-not-exist')
    page._auto_timer.stop()
    assert not page.records.get(second)['preview']['properties'].get('slots')
