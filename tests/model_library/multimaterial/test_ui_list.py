"""C0/C1 through the real panel: states, rows, scope and virtualized lists."""
from prism.widgets.model_material_list import (
    MAX_PREFETCH_THUMBNAILS, PHASE_FAILED, PHASE_INSPECTING, PHASE_READY,
    ROW_UNUSED, STATUS_MISSING_TEXTURE, STATUS_PARTIAL, build_rows, site_count_text,
    summarize)
from tests.model_library.multimaterial.conftest import (
    chair_catalog, make_target, modified_catalog)


def _rows_by_key(rows):
    return {row.key: row for row in rows}


def test_catalog_install_selects_first_material_without_extra_click(loaded, port):
    first = next(row for row in loaded.rows() if row.working_id and row.selectable)
    assert loaded.selected_working_id == first.working_id
    assert loaded.materials.selected_row().working_id == first.working_id
    assert not port.calls_of('highlight'), 'Automatic selection must leave the model unobscured'


def test_compact_cards_three_columns_and_f2_rename(loaded, port, qtbot, monkeypatch):
    import json
    from PyQt6 import QtCore, QtWidgets
    loaded.resize(380, 900)
    loaded.show()
    loaded.select_material('w-wood')
    view = loaded.materials
    qtbot.waitUntil(lambda: view.viewport().width() > 100)
    qtbot.waitUntil(lambda: view.gridSize().width() == (view.viewport().width() - 2) // 3)
    qtbot.waitUntil(lambda: view.visualRect(view.model().index(2, 0)).y()
                   == view.visualRect(view.model().index(0, 0)).y())
    assert loaded.heading.isHidden() and loaded.status.isHidden()
    view.setFocus()
    qtbot.keyClick(view, QtCore.Qt.Key.Key_F2)
    editor = view.findChild(QtWidgets.QLineEdit)
    assert editor is not None
    editor.setText('Wood renamed')
    qtbot.keyClick(editor, QtCore.Qt.Key.Key_Return)
    commits = port.calls_of('commit')
    assert commits
    request = commits[-1][1]
    assert request.working_ids == ('w-wood',)
    assert json.loads(request.parameters_json)['patch']['display_name'] == 'Wood renamed'


# ----------------------------------------------------------------------
# States
# ----------------------------------------------------------------------
def test_nothing_claims_a_catalog_before_the_service_answers(qtbot):
    from prism.widgets.model_material_panel import ModelMaterialPanel
    panel = ModelMaterialPanel()
    qtbot.addWidget(panel)
    panel.reset(make_target())
    assert panel.materials.model() is None
    assert not panel.materials.isEnabled()
    assert '尚未解析' in panel.heading.text()
    assert '未接入' in panel.status.text()
    assert all(not button.isEnabled() for button in panel.actions.values())
    assert '端口未接入' in panel.action_reasons['replace']


def test_a_bare_model_id_cannot_be_inspected(loaded, port):
    # A card summary only carries an id: revisions and the session are missing.
    before = len(port.calls_of('inspect'))
    loaded.reset('model-a')
    assert loaded.materials.model() is None
    assert '尚未解析' in loaded.heading.text()
    assert '缺少工程会话与修订信息' in loaded.status.text()
    assert len(port.calls_of('inspect')) == before, '只有 id 不该发起解析'


def test_inspecting_is_a_state_of_its_own(panel, port):
    assert panel.phase() == PHASE_INSPECTING
    assert '解析中' in panel.heading.text()
    assert port.calls_of('inspect'), '完整身份应发起一次解析'


def test_a_failure_is_not_an_empty_material_list(loaded, port):
    loaded.publish_failed(make_target(), '原件损坏')
    assert loaded.phase() == PHASE_FAILED
    assert '解析失败' in loaded.heading.text()
    assert '原件损坏' in loaded.status.text()
    assert '失败不等于' in loaded.status.text()
    assert loaded.materials.model() is None
    assert not loaded.materials.isEnabled()


def test_a_late_result_of_the_previous_session_is_dropped(loaded):
    # A different model (or a different session) is what "previous session"
    # actually means; a higher revision of the *same* model is now legitimately
    # adopted (the service replays commits onto newer state).
    stale = make_target('model-b')
    catalog, state = chair_catalog()
    assert loaded.publish_catalog(stale, catalog, state) is False
    assert loaded.stale_publications() == 1
    assert loaded.phase() == PHASE_READY
    # The fresh one is accepted.
    assert loaded.publish_catalog(make_target(), catalog, state) is True


def test_unresolved_entries_are_named_not_counted_as_zero(loaded):
    catalog, state = chair_catalog(unresolved=('legacy_material_slot', 'mat-9'))
    loaded.publish_catalog(make_target(), catalog, state)
    rows = _rows_by_key(build_rows(catalog, state))
    unresolved = [row for row in rows.values() if row.kind == 'unresolved']
    assert len(unresolved) == 2
    assert all(row.site_count is None for row in unresolved)
    assert all('未解析' in row.badges() for row in unresolved)
    assert site_count_text(None) == '—'


# ----------------------------------------------------------------------
# Rows and scope
# ----------------------------------------------------------------------
def test_shared_material_states_how_many_sites_it_affects(loaded):
    loaded.select_material('w-wood')
    assert '影响 4 处' in loaded.scope.text()
    row = loaded.material_row('w-wood')
    assert row.site_count == 4
    assert row.impact_text().endswith('影响 4 处')


def test_row_conditions_are_shown_per_kind(loaded):
    rows = _rows_by_key(loaded.rows())
    assert rows['working:w-wood'].status == 'editable'
    assert rows['working:w-broken'].status == STATUS_MISSING_TEXTURE
    assert '缺图' in ' '.join(rows['working:w-broken'].badges())
    assert '贴图文件缺失' in rows['working:w-broken'].detail
    assert rows['working:w-partial'].status == STATUS_PARTIAL
    assert '部分支持' in ' '.join(rows['working:w-partial'].badges())


def test_a_texture_without_a_reported_state_is_not_called_missing(loaded):
    """An empty resource id is not a missing file; silence is not a verdict."""
    from prism.model_library.multimaterial.contracts import (
        MaterialAssignment, MaterialCatalog, MaterialDefinition, MaterialState,
        TextureUse, WorkingMaterial)
    from prism.widgets.model_material_list import (
        STATUS_TEXTURE_UNKNOWN, build_rows as build, texture_states)
    definition = MaterialDefinition(
        'mat-x', 'src-x', '未知状态材质', 'pbr',
        textures=(TextureUse('basecolor', 'res-x', 'RGB', 0, 'srgb'),))
    catalog = MaterialCatalog(1, 1, 'digest', 'parser', (definition,), ())
    state = MaterialState(make_target(),
                          (WorkingMaterial('w-x', 'mat-x', '未知状态材质'),),
                          (MaterialAssignment('bind-x', 'w-x'),))
    row = _rows_by_key(build(catalog, state))['working:w-x']
    assert row.status == STATUS_TEXTURE_UNKNOWN
    assert row.missing_slots == ()
    assert '贴图状态未取得' in row.detail
    assert '缺图' not in ' '.join(row.badges())
    assert texture_states(definition) == {'basecolor': 'unknown'}


def test_source_only_definitions_do_not_add_slot_cards(loaded):
    rows = loaded.rows()
    assert not [row for row in rows if row.kind == ROW_UNUSED]
    assert len([row for row in rows if row.working_id]) == len(loaded._state.materials)


def test_collapsed_shared_slots_stay_compact_in_tall_inspector(loaded, qtbot):
    loaded.resize(400, 1350)
    loaded.show()
    loaded.select_material('w-wood')
    slots = list(loaded.editor.slots.values())
    for slot in slots:
        slot.fold.setChecked(False)
    qtbot.wait(50)
    for first, second in zip(slots, slots[1:]):
        assert 0 < second.y() - first.y() < 65
    assert slots[-1].y() - slots[0].y() < 520


def test_sites_without_any_material_are_listed_separately(loaded):
    rows = _rows_by_key(loaded.rows())
    group = rows['unassigned']
    assert group.site_count == 1
    assert '未指定材质' in group.impact_text()
    assert 'back' in group.detail


def test_two_materials_with_the_same_name_stay_separate(loaded, qtbot, port):
    catalog, state = chair_catalog()
    from prism.model_library.multimaterial.contracts import (
        MaterialAssignment, MaterialState, WorkingMaterial)
    materials = state.materials + (
        WorkingMaterial('w-wood-copy', 'mat-wood', '木材'),)
    state = MaterialState(state.target, materials,
                          state.assignments + (MaterialAssignment('bind-back', 'w-wood-copy'),))
    loaded.publish_catalog(make_target(), catalog, state)
    rows = _rows_by_key(loaded.rows())
    assert rows['working:w-wood'].working_id == 'w-wood'
    assert rows['working:w-wood-copy'].working_id == 'w-wood-copy'
    loaded.select_material('w-wood-copy')
    assert loaded.selected_working_id == 'w-wood-copy'
    assert '影响 1 处' in loaded.scope.text()
    loaded.select_material('w-wood')
    assert '影响 4 处' in loaded.scope.text()
    assert port.calls_of('preview') == []


def test_a_material_selection_is_not_a_site_selection(loaded, port, monkeypatch):
    loaded.select_material('w-wood')
    assert loaded.selected_binding_id is None
    assert not loaded.action_reasons.get('unique')
    assert loaded.actions['unique'].isEnabled()
    from PyQt6 import QtWidgets
    monkeypatch.setattr(QtWidgets.QInputDialog, 'getItem',
                        lambda *args: ('', False))
    loaded.make_unique()
    assert loaded.selected_binding_id is None
    assert port.calls_of('commit') == []
    # The whole material was highlighted for orientation only.
    highlight = port.calls_of('highlight')
    assert highlight and len(highlight[-1][2]) == 4
    assert port.calls_of('commit') == [], '高亮不是提交'


def test_selecting_one_site_enables_only_that_site(qtbot, loaded, port):
    loaded.select_material('w-wood')
    assert loaded.select_site('bind-leg-bl') is True
    assert loaded.selected_binding_id == 'bind-leg-bl'
    assert loaded.actions['unique'].isEnabled()
    job = loaded.make_unique()
    assert job
    request = port.calls_of('commit')[-1][1]
    assert tuple(request.binding_ids) == ('bind-leg-bl',)
    assert tuple(request.working_ids) == ('w-wood',)
    assert request.operation.value == 'make_unique'


def test_a_pick_from_the_large_preview_matches_by_binding_id(loaded):
    loaded.select_material('w-wood')
    assert loaded.publish_pick_result(make_target(), 'bind-leg-fl') is True
    assert loaded.selected_binding_id == 'bind-leg-fl'
    # A site of another material switches the material first, then selects the
    # site: the pick is resolved against the whole assignment table.
    assert loaded.publish_pick_result(make_target(), 'bind-seat') is True
    assert loaded.selected_working_id == 'w-metal'
    assert loaded.selected_binding_id == 'bind-seat'
    assert '影响 1 处' in loaded.scope.text()


def test_a_pick_of_an_unassigned_site_lands_on_the_unassigned_group(loaded):
    loaded.select_material('w-wood')
    assert loaded.publish_pick_result(make_target(), 'bind-back') is True
    assert loaded.selected_working_id is None
    assert '未指定材质' in loaded.site_label.text()


def test_a_pick_of_a_vanished_site_changes_nothing(loaded):
    loaded.select_material('w-wood')
    assert loaded.publish_pick_result(make_target(), 'bind-deleted') is False
    assert '已过期' in loaded.site_label.text()
    assert loaded.selected_working_id == 'w-wood'
    assert loaded.selected_binding_id is None


def test_an_ambiguous_transparent_pick_is_never_guessed(loaded):
    loaded.select_material('w-wood')
    handled = loaded.publish_pick_result(
        make_target(), 'bind-leg-fl', candidates=('bind-leg-fr',))
    assert handled is False
    assert '重叠候选' in loaded.site_label.text()
    assert loaded.selected_binding_id is None, '有多个候选时不能替用户挑一个'
    assert loaded.candidates() == ('bind-leg-fl', 'bind-leg-fr')


# ----------------------------------------------------------------------
# Scale
# ----------------------------------------------------------------------
def test_a_thousand_material_rows_stay_virtualized(qtbot):
    from prism.model_library.multimaterial.contracts import (
        MaterialAssignment, MaterialCatalog, MaterialDefinition, MaterialState,
        BindingSite, WorkingMaterial)
    definitions = tuple(
        MaterialDefinition(f'mat-{index}', f'src-{index}', f'材质 {index}', 'pbr')
        for index in range(1000))
    sites = tuple(BindingSite(f'bind-{index}', f'src-{index}', f'节点 {index}',
                              'geo', 'prim', f'mat-{index}') for index in range(1000))
    materials = tuple(WorkingMaterial(f'w-{index}', f'mat-{index}', f'材质 {index}')
                      for index in range(1000))
    assignments = tuple(MaterialAssignment(f'bind-{index}', f'w-{index}')
                        for index in range(1000))
    catalog = MaterialCatalog(1, 1, 'digest', 'parser', definitions, sites)
    state = MaterialState(make_target(), materials, assignments)
    rows = build_rows(catalog, state)
    assert len(rows) == 1000
    assert '1,000 个材质' in summarize(rows)

    from prism.widgets.model_material_list import MaterialListModel
    model = MaterialListModel()
    requests = []
    model.thumbnail_requested.connect(requests.append)
    model.set_rows(rows)
    assert model.rowCount() == 1000
    # Painting a window of rows must not schedule a read per row.
    for position in range(40):
        model.data(model.index(position, 0), 0x0001)  # DecorationRole
    assert len(requests) <= MAX_PREFETCH_THUMBNAILS
    assert len(model.pending_thumbnails()) <= MAX_PREFETCH_THUMBNAILS
    # Repeated paints of the same rows do not duplicate requests.
    for position in range(40):
        model.data(model.index(position, 0), 0x0001)
    assert len(requests) <= MAX_PREFETCH_THUMBNAILS


def test_a_stale_thumbnail_does_not_land_in_the_new_list(qtbot):
    from prism.widgets.model_material_list import MaterialListModel
    catalog, state = chair_catalog()
    rows = build_rows(catalog, state)
    model = MaterialListModel()
    model.set_rows(rows)
    generation = model.generation()
    model.set_rows(())
    assert model.set_thumbnail(rows[0].thumbnail_key or 'working:w-wood',
                               None, generation) is False


# ----------------------------------------------------------------------
# Local modifications and the export statement
# ----------------------------------------------------------------------
def test_local_modifications_are_announced_before_export(loaded):
    assert loaded.export_notice.isVisibleTo(loaded) is False
    catalog, state = modified_catalog()
    loaded.publish_catalog(make_target(), catalog, state)
    assert loaded.export_notice.isVisibleTo(loaded) is False
    assert '原件导出不包含 Prism 外观修改' in loaded.export_notice.text()
