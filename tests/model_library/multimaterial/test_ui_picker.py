"""C3 through the real picker: cards, tag search, temporary preview, one apply."""
import json

from PyQt6 import QtCore

from prism.model_library.multimaterial.contracts import MaterialCapabilityReport
from prism.widgets.model_material_list import LIBRARY_MIME
from prism.widgets.model_material_picker import (
    MODE_FULL, MODE_SURFACE, MaterialPicker, PRESERVED, SURFACE_CHANNELS)


class _DragEvent:
    """Minimal stand-in for a QDragEnterEvent/QDropEvent."""

    def __init__(self, mime):
        self._mime = mime
        self.accepted = False
        self.ignored = False
        self.action = None

    def mimeData(self):
        return self._mime

    def setDropAction(self, action):
        self.action = action

    def accept(self):
        self.accepted = True

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True

    def position(self):
        return QtCore.QPointF(4.0, 4.0)


def _library_mime(ids, kind='material'):
    mime = QtCore.QMimeData()
    mime.setData(LIBRARY_MIME, json.dumps(
        {'project': 'project-a', 'scope': '1', 'kind': kind, 'ids': list(ids)}
    ).encode('utf-8'))
    return mime


def _open(panel):
    panel.select_material('w-wood')
    picker = panel.build_picker()
    assert picker is not None
    return picker


def _click_card(picker, entry_id):
    picker._cards.selection_toggled.emit(entry_id, False)


def _preview_params(port):
    return json.loads(port.calls_of('preview')[-1][1].parameters_json)['replace']


# ----------------------------------------------------------------------
# Reuse of the existing cards, thumbnails and tag search
# ----------------------------------------------------------------------
def test_the_picker_reuses_cards_and_library_tags(loaded, port):
    picker = _open(loaded)
    assert picker._cards is not None
    assert [asset['id'] for asset in picker._cards.assets] == ['lib-1', 'lib-2', 'lib-3']
    assert set(picker.tag_buttons) == {'木材', '室内', '金属', '织物'}
    picker.tag_buttons['金属'].setChecked(True)
    assert [entry.entry_id for entry in picker.visible_entries()] == ['lib-2']
    picker.tag_buttons['金属'].setChecked(False)
    picker.search.setText('胡桃')
    assert [entry.entry_id for entry in picker.visible_entries()] == ['lib-1']
    assert port.calls_of('commit') == []


def test_card_thumbnails_are_read_through_the_library_source(loaded):
    picker = _open(loaded)
    picker.thumbnail_requested.emit('lib-1')
    assert loaded.library_source.reads == ['lib-1']
    assert picker._cards.patched['lib-1'].startswith('data:image/png')


def test_the_existing_record_path_is_used_when_the_source_has_records(qtbot, port):
    """With raw material records the picker reuses ``set_records`` semantics."""
    from prism.widgets.model_material_panel import ModelMaterialPanel
    from prism.widgets.model_material_picker import LibraryEntry, MaterialPicker
    from tests.model_library.multimaterial.conftest import (
        FakeCards, chair_catalog, make_target)

    class RecordingCards(FakeCards):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.records = []
            self.asked = []

        def set_records(self, records, selected_id=None, thumbnail_for=None):
            self.records = list(records)
            if thumbnail_for is not None:
                for record in records:
                    self.asked.append(thumbnail_for(record))

    class RecordSource:
        def records(self):
            return [{'id': 'lib-9', 'name': '麻布', 'tags': ['织物'],
                     'textures': {'basecolor': {'name': 'linen.png'}}}]

        def entries(self):
            return (LibraryEntry('lib-9', '麻布', tags=('织物',),
                                 snapshot_token='sha256:dd44'),)

        def thumbnail(self, entry_id):
            return 'data:image/png;base64,BBBB'

    panel = ModelMaterialPanel()
    qtbot.addWidget(panel)
    panel.set_port(port)
    panel.set_library_source(RecordSource())
    panel.picker_factory = lambda parent=None: MaterialPicker(
        parent, card_factory=RecordingCards)
    catalog, state = chair_catalog()
    panel.reset(make_target())
    panel.publish_catalog(make_target(), catalog, state)
    panel.select_material('w-wood')
    picker = panel.build_picker()
    assert [record['id'] for record in picker._cards.records] == ['lib-9']
    assert picker._cards.asked == [None], '缺图时先要一次缓存缩略图，不猜'
    picker._card_focused('lib-9')
    assert port.calls_of('preview'), '卡片聚焦仍然走预览'
    assert port.calls_of('commit') == []

# ----------------------------------------------------------------------
# Preview is temporary
# ----------------------------------------------------------------------
def test_selecting_a_card_previews_without_committing(loaded, port):
    picker = _open(loaded)
    _click_card(picker, 'lib-1')
    previews = port.calls_of('preview')
    assert previews, '选择卡片必须先预览'
    request = previews[-1][1]
    assert request.operation.value == MODE_SURFACE
    assert tuple(request.working_ids) == ('w-wood',)
    assert port.calls_of('commit') == [], '预览不能提交'
    assert '未提交' in loaded.preview_status.text()


def test_cancelling_drops_the_draft_and_asks_for_the_committed_look(loaded, port):
    picker = _open(loaded)
    _click_card(picker, 'lib-1')
    picker.reject()
    assert port.calls_of('commit') == []
    assert port.calls_of('cancel'), '取消必须撤回自己的预览'
    # Cancelling a preview is not the same as restoring the picture: the panel
    # asks the backend for the committed appearance and says so.
    assert port.calls_of('restore'), '取消后必须请求恢复已提交外观'
    assert '正在恢复已提交外观' in loaded.preview_status.text()


def test_cancelling_without_a_restore_port_says_the_screen_may_be_stale(loaded, port):
    port.restore = None
    picker = _open(loaded)
    _click_card(picker, 'lib-1')
    picker.reject()
    assert port.calls_of('cancel')
    assert '未提供' in loaded.preview_status.text()
    assert '草稿帧' in loaded.preview_status.text()


def test_closing_the_dialog_never_commits(loaded, port):
    picker = _open(loaded)
    _click_card(picker, 'lib-2')
    picker.close()
    assert port.calls_of('commit') == []


# ----------------------------------------------------------------------
# Apply: one transaction, and the alpha promise travels with it
# ----------------------------------------------------------------------
def test_apply_commits_exactly_one_surface_replacement(loaded, port):
    picker = _open(loaded)
    _click_card(picker, 'lib-1')
    picker.surface_mode.setChecked(True)
    picker._apply()
    commits = port.calls_of('commit')
    assert len(commits) == 1
    request = commits[0][1]
    assert request.operation.value == MODE_SURFACE
    parameters = json.loads(request.parameters_json)['replace']
    assert parameters['entry_id'] == 'lib-1'
    # Preview and apply name the same frozen library content.
    assert parameters['snapshot_token'] == 'sha256:aa11'
    assert 'preserve_alpha' not in parameters, '保留规则由服务端执行，界面不再自行推断'
    assert '已提交材质库替换' in loaded.save_status.text()
    assert '已保存' not in loaded.save_status.text()


def test_the_picker_states_what_a_surface_replacement_keeps(loaded):
    picker = _open(loaded)
    _click_card(picker, 'lib-1')
    note = picker.mode_note.text()
    for word in PRESERVED:
        assert word in note
    for word in SURFACE_CHANNELS:
        assert word in note
    # The snapshot promise and the "a sphere is not the model" caveat are stated.
    assert '快照' in note
    assert '材质球不等于模型本体' in note
    assert picker.alpha_note.isVisibleTo(picker) is True
    assert '镂空' in picker.alpha_note.text()


def test_full_replacement_is_an_explicit_advanced_choice(loaded, port):
    picker = _open(loaded)
    _click_card(picker, 'lib-1')
    picker.full_mode.setChecked(True)
    assert picker.full_note.isVisibleTo(picker) is True
    assert '完整替换会改变' in picker.full_note.text()
    assert '预览中' in loaded.preview_status.text(), '切换方式要重新预览'
    picker._apply()
    request = port.calls_of('commit')[-1][1]
    assert request.operation.value == MODE_FULL
    assert json.loads(request.parameters_json)['replace']['mode'] == MODE_FULL


# ----------------------------------------------------------------------
# No target, no drop, no apply
# ----------------------------------------------------------------------
def test_without_a_selected_material_no_picker_opens(loaded, port):
    loaded.materials.clear_selection()
    assert loaded.build_picker() is None
    before = len(port.calls_of('commit'))
    loaded.open_picker()
    assert len(port.calls_of('commit')) == before
    assert '无法打开材质库选择器' in loaded.save_status.text()


def test_an_explicit_drop_previews_and_a_drop_without_a_row_does_nothing(loaded, port):
    view = loaded.materials
    model = view.material_model()
    view.indexAt = lambda point: model.index_for_key('working:w-wood')
    accepted = _DragEvent(MaterialPicker.mime_data('lib-1'))
    view.dragEnterEvent(accepted)
    assert accepted.accepted, '命中明确材质行时必须接受拖放'
    previews = len(port.calls_of('preview'))
    drop = _DragEvent(MaterialPicker.mime_data('lib-1'))
    view.dropEvent(drop)
    assert len(port.calls_of('preview')) == previews + 1
    assert loaded.selected_working_id == 'w-wood'
    assert port.calls_of('commit') == [], '拖放也只预览'

    view.indexAt = lambda point: QtCore.QModelIndex()
    refused = _DragEvent(MaterialPicker.mime_data('lib-1'))
    view.dragEnterEvent(refused)
    assert refused.ignored is True, '没有命中行时不能覆盖全模型'
    assert not refused.accepted


def test_a_drop_on_a_non_material_row_is_refused(loaded, port):
    assert loaded._row_dropped('unassigned', 'lib-1') is False
    assert port.calls_of('preview') == []


def test_the_existing_library_drag_payload_is_reused(loaded, port):
    view = loaded.materials
    model = view.material_model()
    view.indexAt = lambda point: model.index_for_key('working:w-wood')
    single = _DragEvent(_library_mime(['lib-3']))
    view.dragEnterEvent(single)
    assert single.accepted is True
    assert single.action == QtCore.Qt.DropAction.CopyAction, '拖入是复制，不移动库记录'
    view.dropEvent(single)
    assert json.loads(port.calls_of('preview')[-1][1].parameters_json)[
        'replace']['entry_id'] == 'lib-3'
    # A batch of cards is not a batch of materials to replace.
    batch = _DragEvent(_library_mime(['lib-1', 'lib-2']))
    view.dragEnterEvent(batch)
    assert batch.ignored is True
    # A model drag is not a material drag.
    model_drag = _DragEvent(_library_mime(['model-1'], kind='model'))
    view.dragEnterEvent(model_drag)
    assert model_drag.ignored is True


def test_an_unavailable_capability_is_stated_in_the_picker(loaded, port):
    port.report = MaterialCapabilityReport(
        read_catalog=True, edit_material=False, reason='后端未验证材质库替换')
    loaded.set_capabilities(port.report)
    loaded.select_material('w-wood')
    picker = loaded.build_picker()
    assert picker.blocked_note.isVisibleTo(picker) is True
    assert '后端未验证材质库替换' in picker.blocked_note.text()
    assert not picker.apply.isEnabled()
    _click_card(picker, 'lib-1')
    assert not picker.apply.isEnabled()
    assert '预览不可用' in picker.status.text()
    assert port.calls_of('preview') == []
