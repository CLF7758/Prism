"""Exercise SurfaceStudio's actual HTML cards and the Qt selection bridge."""
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt


def javascript(grid, expression, qtbot):
    result = []
    grid.web.page().runJavaScript(expression, result.append)
    qtbot.waitUntil(lambda: bool(result), timeout=10000)
    return result[0]


def show_cards(window, qtbot):
    window.resize(1440, 900)
    window.show()
    window.open_material_library()
    page = window.material_library
    records = [page.records.create(name=name) for name in ('木头', '金属', '树叶')]
    qtbot.waitUntil(lambda: page.materials.ready, timeout=15000)
    qtbot.waitUntil(lambda: javascript(page.materials,
        "document.querySelectorAll('.asset-card').length", qtbot) == 3, timeout=10000)
    return page, records


def test_surface_cards_render_and_click_selects_same_record(main_window, qtbot):
    page, records = show_cards(main_window, qtbot)
    grid = page.materials
    assert javascript(grid, "getComputedStyle(document.getElementById('asset-grid')).display", qtbot) == 'grid'
    assert javascript(grid, "document.querySelectorAll('.card-image img').length", qtbot) == 3
    javascript(grid, "document.querySelectorAll('.asset-card')[1].click()", qtbot)
    qtbot.waitUntil(lambda: page.selected_id == records[1]['id'])
    assert page.details.name.text() == '金属'
    assert javascript(grid, "document.querySelector('.asset-card.selected h3').textContent", qtbot) == '金属'


def test_view_controls_and_keyboard_selection(main_window, qtbot):
    page, records = show_cards(main_window, qtbot)
    grid = page.materials
    qtbot.mouseClick(page.list_button, Qt.MouseButton.LeftButton)
    assert javascript(grid, "document.getElementById('asset-grid').classList.contains('list-mode')", qtbot)
    page.card_size.setValue(280)
    assert javascript(grid, "getComputedStyle(document.documentElement).getPropertyValue('--card-size')", qtbot).strip() == '280px'
    javascript(grid, "document.querySelector('.asset-card').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))", qtbot)
    qtbot.waitUntil(lambda: page.selected_id == records[0]['id'])


def test_record_text_is_escaped_and_stale_browser_click_is_ignored(main_window, qtbot):
    page, records = show_cards(main_window, qtbot)
    grid = page.materials
    name = '<img src=x onerror="window.injected=1">'
    page.records.update(records[0]['id'], name=name, notes='')
    qtbot.wait(100)
    assert javascript(grid, "document.querySelector('.card-info h3').textContent", qtbot) == name
    assert javascript(grid, "Boolean(window.injected)", qtbot) is False
    generation = grid.generation
    page.records.create(name='第四条')
    grid.bridge.choose(grid._dom_id(records[0]['id']), generation)
    assert page.selected_id is None


def test_inspector_shows_recorded_versions_tags_and_properties(main_window, qtbot):
    window = main_window
    window.open_material_library()
    page = window.material_library
    record = page.records.create(name='树叶')
    stored = page.records.records[0]
    stored.update(tags=['植物', '裁切'], assetVersion='R0002',
                  nativePackages=[{'engineVersion': '5.3.2'}],
                  textures={'Albedo': {'name': 'leaf_color.png', 'width': 1024, 'height': 1024}},
                  preview={'properties': {'blendMode': 'masked', 'roughness': 0.7, 'sss': True}})
    page.refresh()
    page.materials.select_record(record['id'])
    assert page.details.info['engine'].text() == '5.3.2'
    assert page.details.info['revision'].text() == 'R0002'
    assert page.details.properties['blendMode'].text() == '透明裁切'
    assert page.details.properties['roughness'].text() == '0.7'
    assert page.details.properties['sss'].text() == '开启'
    assert 'leaf_color.png' in page.details.texture_info.text()
    assert '1024 × 1024' in page.details.texture_info.text()
    # The latest inspector design explicitly includes material sliders.
    assert page.details.slots['roughness'].slider is not None
    buttons = page.details.tags.findChildren(QtWidgets.QToolButton)
    assert {button.text() for button in buttons} == {'植物', '裁切'}
    buttons[0].click()
    assert page.search.text() in {'植物', '裁切'}
    assert page.materials.count() == 1


def test_old_browser_messages_cannot_select_in_a_new_project(main_window, qtbot):
    import copy
    page, records = show_cards(main_window, qtbot)
    grid = page.materials
    old_generation = grid.generation
    scene = main_window.view.scene
    scene.project_id = 'new-project-with-the-same-imported-records'
    scene.material_records = copy.deepcopy(page.records.records)
    for record in scene.material_records:
        record['projectId'] = scene.project_id
    scene.material_library_state = {}
    scene.workspace_nodes = []
    main_window.view.category_panel._install_resource_service()
    page.load_from_scene()
    assert grid.generation > old_generation
    grid.bridge.choose(grid._dom_id(records[0]['id']), old_generation)
    assert page.selected_id is None


def test_imported_record_ids_are_not_inserted_as_html(main_window, qtbot):
    page, _records = show_cards(main_window, qtbot)
    malicious_id = '\"><img src=x onerror="window.idInjected=1">'
    page.records.records[0]['id'] = malicious_id
    page.refresh()
    qtbot.wait(100)
    assert javascript(page.materials, "Boolean(window.idInjected)", qtbot) is False
    javascript(page.materials, "document.querySelector('.asset-card').click()", qtbot)
    qtbot.waitUntil(lambda: page.selected_id == malicious_id)
