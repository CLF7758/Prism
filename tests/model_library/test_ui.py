"""C1/C2/C3: the model page as a bounded window over a larger library.

Three properties are checked here, because they are the ones a user notices
when they break:

* every state has a name and "not connected" never reads as "import worked";
* the list is a window: the first page and the last page are both reachable,
  and the number of installed cards stays inside the budget;
* a query, a cover or a selection of an older project never lands in the new
  one.

The repository and the service below only exist in tests. Production has no
in-memory substitute: an unimplemented port must stay disabled.
"""
import pytest
from PyQt6 import QtCore, QtWidgets

from prism.model_library.contracts import (
    ModelAppearance, ModelCapabilities, ModelCardSummary, ModelFileRef, ModelPage,
    ModelQuery, ModelRecord, ModelStatistics,
)
from prism.model_library.selection import QueryIdentity, SelectionSnapshot
from prism.model_library.service import ModelLibraryService
from prism.widgets.model_library import (
    MAX_WINDOW_PAGES, PAGE_SIZE, ModelLibrary, ModelLibraryState)


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------
class FakeRepository:
    """Metadata store that lives only for the duration of a test."""

    def __init__(self, count=0, *, statistics=None, fail=None):
        self.count = count
        self.statistics = statistics or ModelStatistics()
        self.fail = fail
        self.queries = []

    def _names(self, text):
        names = ['model-%03d' % index for index in range(self.count)]
        return [name for name in names if not text or text in name]

    def query(self, query):
        self.queries.append(query)
        if self.fail is not None:
            raise self.fail
        names = self._names(query.text)
        page = names[query.offset:query.offset + query.limit]
        return ModelPage(
            tuple(ModelCardSummary(name, name, 'fbx', size_bytes=1024,
                                   statistics=self.statistics)
                  for name in page),
            len(names), query.offset)

    def get(self, model_id):
        if not model_id:
            return None
        return ModelRecord(
            model_id=model_id, project_id='project-a', name=model_id,
            main_file_id='file-1', format='fbx', source_revision=2,
            record_revision=5, tags=('椅子',), notes='备注',
            files=(ModelFileRef('file-1', 'chair.fbx', 'model', 2048,
                                'sha-1', 'source-1'),
                   ModelFileRef('file-2', 'chair.png', 'texture', 512,
                                'sha-2', 'source-2')),
            appearance=ModelAppearance(revision=3),
            statistics=self.statistics)

    def save(self, record, *, expected_revision):
        raise AssertionError('模型库页面不得直接写仓库')


class FakeService:
    """Service with real ports recorded, so the UI wiring can be inspected."""

    def __init__(self, repository=None, capabilities=None):
        self.repository = repository
        self.calls = []
        self._capabilities = capabilities or ModelCapabilities(
            browse=repository is not None)

    @property
    def capabilities(self):
        return self._capabilities

    def query(self, query):
        return self.repository.query(query)

    def get(self, model_id):
        return self.repository.get(model_id)

    def import_paths(self, paths, *, folder_id=None):
        self.calls.append(('import_paths', tuple(paths), folder_id))
        return 'job-import-1'

    def export_originals(self, model_ids, destination):
        self.calls.append(('export_originals', tuple(model_ids), destination))
        return 'job-export-1'

    def open_preview(self, model_id):
        self.calls.append(('open_preview', model_id))
        return 'job-preview-1'

    def update_appearance(self, model_id, appearance):
        self.calls.append(('update_appearance', model_id, appearance))
        return 'job-appearance-1'

    def update_metadata(self, model_id, *, tags, notes, expected_revision):
        self.calls.append(('update_metadata', model_id, tags, notes,
                           expected_revision))
        return 'job-metadata-1'

    def create_folder(self, parent_id=None):
        self.calls.append(('create_folder', parent_id))
        return {'id': 'folder-1'}


class FakeSettings:
    """In-memory stand-in for the window's QSettings."""

    def __init__(self):
        self.values = {}

    def setValue(self, key, value):
        self.values[key] = value

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)


class FakeView:
    def __init__(self, project_id='project-a', epoch=1):
        self.scene = QtCore.QObject()
        self.scene.project_id = project_id
        self._trace_open_epoch = epoch
        self.settings = FakeSettings()
        from types import SimpleNamespace
        self.recorded_usage = []
        self.category_panel = SimpleNamespace(record_resource_usage=lambda *args:self.recorded_usage.append(args))


class FakeHost(QtWidgets.QWidget):
    """Stands in for the main window: the page only needs ``view``."""

    def __init__(self, project_id='project-a', epoch=1):
        super().__init__()
        self.view = FakeView(project_id, epoch)


@pytest.fixture
def host(qtbot):
    widget = FakeHost()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def page(qtbot, host):
    widget = ModelLibrary(host)
    qtbot.addWidget(widget)
    yield widget
    pool = getattr(widget, '_pool', None)
    if pool is not None:
        pool.waitForDone(5000)
    widget.close()


def _load(page, qtbot, repository, *, text=''):
    """Install the first page through the real (background) query path."""
    page.search.setText(text)
    page.set_service(ModelLibraryService(repository))
    page.bind_scope('project-a', 1)
    qtbot.waitUntil(lambda: page.state != ModelLibraryState.LOADING, timeout=20000)
    return page


def _wait_until_settled(qtbot, page):
    qtbot.waitUntil(lambda: page.state != ModelLibraryState.LOADING, timeout=20000)


# ----------------------------------------------------------------------
# C1: states
# ----------------------------------------------------------------------
def test_without_a_service_nothing_claims_success(page):
    page.refresh()

    assert page.state == ModelLibraryState.UNAVAILABLE
    assert '未接入' in page.status.text()
    assert '成功' not in page.status.text()
    assert '完成' not in page.status.text()
    assert page.cards is None, '数据服务未接入时不应建立卡片表面'
    assert page.empty_state.isVisibleTo(page)
    assert '未接入' in page.empty_hint.text()
    for button in (page.import_button, page.folder_button, page.export_button):
        assert not button.isEnabled()
        assert '未接入' in button.toolTip(), '禁用要说清楚为什么'
    assert not page.search.isEnabled()


def test_an_empty_library_is_not_the_same_state_as_a_query_without_hits(
        page, qtbot):
    empty = FakeRepository(0)
    _load(page, qtbot, empty)
    assert page.state == ModelLibraryState.EMPTY
    assert '还没有导入' in page.status.text()

    filtered = FakeRepository(4)
    _load(page, qtbot, filtered, text='no-such-model')
    assert page.state == ModelLibraryState.NO_RESULTS
    assert '没有符合条件' in page.status.text()
    assert _wait_until_settled(qtbot, page) is None


def test_a_broken_repository_reports_failure_not_an_empty_library(page, qtbot):
    _load(page, qtbot, FakeRepository(3, fail=RuntimeError('数据库损坏')))
    assert page.state == ModelLibraryState.ERROR
    assert '失败' in page.status.text()
    assert '数据库损坏' in page.status.text()
    assert '未被修改' in page.empty_hint.text()


def test_an_unknown_statistic_is_shown_as_unknown_not_as_zero(page, qtbot):
    repository = FakeRepository(1, statistics=ModelStatistics(basis='cells'))
    _load(page, qtbot, repository)
    page._selection_toggled('model-000', False)

    details = page.details
    assert details.info['triangles'].text() == '未取得'
    assert details.info['vertices'].text() == '未取得'
    # The basis travels with the number: cells are never called triangles.
    assert details.info['basis'].text() == 'cells'
    assert details.info['size'].text() != '0'


def test_the_details_panel_reads_the_full_record_not_the_card_summary(page, qtbot):
    _load(page, qtbot, FakeRepository(2))
    page._selection_toggled('model-001', False)

    details = page.details
    qtbot.waitUntil(lambda: details._record is not None and details._record.model_id == 'model-001')
    assert details.info['revision'].text() == '2'
    assert details.info['size'].text() == '2,048 字节', '原件大小取自主文件'
    assert details.info['dependencies'].text() == '2 个文件齐全'
    assert details.files.count() == 2
    assert details.tags.text() == '椅子'
    assert details.notes.toPlainText() == '备注'


def test_a_record_without_a_main_file_reports_an_unknown_size(page, qtbot):
    repository = FakeRepository(1)
    _load(page, qtbot, repository)
    record = repository.get('model-000')
    orphan = ModelRecord(model_id='model-000', project_id='project-a',
                         name='model-000', main_file_id='missing',
                         format='fbx', files=record.files)
    page.details.show_record(orphan)
    assert page.details.info['size'].text() == '未取得'
    assert page.details.info['dependencies'].text() == '2 个文件齐全'


# ----------------------------------------------------------------------
# C2: paging, selection, covers
# ----------------------------------------------------------------------
def test_the_first_page_and_the_last_page_are_both_reachable(page, qtbot):
    repository = FakeRepository(300)
    _load(page, qtbot, repository)

    seen = set(page._summaries)
    last_index = (300 - 1) // PAGE_SIZE
    for _ in range(2 * last_index + 2):
        if max(page._pages) >= last_index:
            break
        before = max(page._pages)
        page.load_more()
        qtbot.waitUntil(lambda: max(page._pages) > before, timeout=20000)
        seen.update(page._summaries)

    assert last_index in page._pages, '末页必须可达'
    assert page._total == 300
    assert len(seen) == 300, '每一页的模型都必须被访问过，不能只给前 %d 个' % PAGE_SIZE
    assert len(page._pages) <= MAX_WINDOW_PAGES
    assert len(page._summaries) <= MAX_WINDOW_PAGES * PAGE_SIZE


def test_the_installed_card_surface_stays_inside_the_dom_budget(page, qtbot):
    _load(page, qtbot, FakeRepository(300))
    last_index = (300 - 1) // PAGE_SIZE
    while max(page._pages) < last_index:
        before = max(page._pages)
        page.load_more()
        qtbot.waitUntil(lambda: max(page._pages) > before, timeout=20000)

    assert len(page.cards.assets) <= 300, 'DOM 目标 ≤300 张可见/预取卡片'


def test_a_result_of_an_older_query_is_refused(page, qtbot):
    repository = FakeRepository(5)
    _load(page, qtbot, repository)
    stale_identity = page._identity
    stale_page = repository.query(ModelQuery(offset=0, limit=PAGE_SIZE))

    page.search.setText('model-002')
    qtbot.waitUntil(lambda: page._identity.matches(
        QueryIdentity('model-002', None, 'name', page._identity.revision)),
        timeout=10000)
    _wait_until_settled(qtbot, page)
    assert list(page._summaries) == ['model-002']

    assert page.publish_page(stale_page, stale_identity) is False
    assert set(page._summaries) == {'model-002'}, '旧查询结果不能盖掉新列表'


def test_a_late_result_of_the_previous_project_is_dropped(page, qtbot):
    repository = FakeRepository(2)
    _load(page, qtbot, repository)
    old_identity = page._identity

    page.bind_scope('project-b', 2, refresh=False)
    assert page.selection.count == 0
    assert page.details.info['format'].text() == '—'

    late = repository.query(ModelQuery(offset=0, limit=PAGE_SIZE))
    assert page.publish_page(late, old_identity) is False
    assert page._summaries == {}


def test_the_selection_bar_states_the_size_of_the_batch(page, qtbot):
    _load(page, qtbot, FakeRepository(4))
    assert page.selection_label.text() == '未选择模型'
    assert not page.selection_bar.isVisibleTo(page)

    page._selection_toggled('model-001', False)
    assert page.selection_bar.isVisibleTo(page)
    assert page.selection_label.text() == '已选择 1 个模型'
    assert page.clear_selection_button.isEnabled()

    page._selection_toggled('model-002', True)
    assert page.selection_label.text() == '已选择 2 个模型'

    page._clear_selection()
    assert page.selection_label.text() == '未选择模型'
    assert not page.clear_selection_button.isEnabled()


def test_a_batch_selection_survives_paging_past_the_first_page(page, qtbot):
    repository = FakeRepository(300)
    _load(page, qtbot, repository)
    page._selection_toggled('model-000', False)
    page._selection_toggled('model-001', True)

    for _ in range(MAX_WINDOW_PAGES):
        before = max(page._pages)
        page.load_more()
        qtbot.waitUntil(lambda: max(page._pages) > before, timeout=20000)

    # The first page left the window, but the picks did not.
    assert 'model-000' not in page._summaries
    assert page.selection.contains('model-000')
    assert page.selection.count == 2
    assert page.selection_label.text() == '已选择 2 个模型'
    assert page.selected_model_ids().model_ids == ('model-000', 'model-001')
    # Only the visible part is mirrored onto the cards.
    assert page.cards.selected_ids <= {'model-000', 'model-001'}


def test_a_cover_comes_from_the_cache_and_never_from_the_original(page, qtbot):
    _load(page, qtbot, FakeRepository(2))
    asset = page.cards.assets[0]
    from prism.widgets.material_cards import placeholder_image
    assert asset['thumbnail'] == placeholder_image({'assetKind':'model'})
    assert asset['thumbnailPending'] is True

    delivered = []
    page.cards.update_thumbnail = lambda record, thumbnail: delivered.append(
        (record['id'], thumbnail))
    page._deliver_cover('model-000')
    assert delivered and delivered[0][1] == placeholder_image({'assetKind':'model'})

    # A summary that carries a cached data URL is passed through untouched.
    page._summaries['model-000'] = ModelCardSummary(
        'model-000', 'model-000', 'fbx', thumbnail_data_url='data:image/png;base64,AA')
    page._deliver_cover('model-000')
    assert delivered[-1][1] == 'data:image/png;base64,AA'


def test_the_view_state_of_a_project_is_remembered(page, qtbot, host):
    repository = FakeRepository(300)
    _load(page, qtbot, repository)
    page._selection_toggled('model-001', False)
    page.list_button.setChecked(True)
    page._set_layout()
    page.card_size.setValue(260)
    page.sort.setCurrentIndex(page.sort.findData('-name'))
    page.cards.set_scroll_position(480)
    assert page._save_view_state()

    reopened = ModelLibrary(host)
    qtbot.addWidget(reopened)
    reopened.set_service(ModelLibraryService(repository))
    reopened.bind_scope(*reopened._host_scope())

    assert reopened.list_button.isChecked(), '布局按项目记住'
    assert reopened.card_size.value() == 260
    assert reopened.sort.currentData() == '-name'
    qtbot.waitUntil(lambda: reopened.cards is not None, timeout=20000)
    assert reopened.cards.scroll_position == 480
    assert reopened.selection.current == 'model-001'

    pool = getattr(reopened, '_pool', None)
    if pool is not None:
        pool.waitForDone(5000)
    reopened.close()


def test_each_folder_keeps_its_own_view_state(page, qtbot):
    _load(page, qtbot, FakeRepository(3))
    page.card_size.setValue(300)
    page._save_view_state()

    page.set_folder('folder-1')
    assert page.card_size.value() == 220, '新目录回到默认卡片大小'

    page.set_folder(None)
    assert page.card_size.value() == 300, '回到根目录要恢复自己的设置'


# ----------------------------------------------------------------------
# C3: import / export / preview handlers
# ----------------------------------------------------------------------
def test_import_stays_disabled_without_the_port(page, qtbot):
    repository = FakeRepository(1)
    service = FakeService(repository, ModelCapabilities(browse=True))
    page.set_service(service)
    page.bind_scope('project-a', 1)
    _wait_until_settled(qtbot, page)

    assert not page.import_button.isEnabled()
    assert page.import_paths(('C:/models/chair.fbx',)) is None
    assert service.calls == []
    assert '尚未接入' in page.status.text()


def test_import_submits_one_job_and_does_not_claim_it_finished(page, qtbot):
    service = FakeService(
        FakeRepository(1),
        ModelCapabilities(browse=True, import_models=True,
                          export_originals=True))
    page.set_service(service)
    page.bind_scope('project-a', 1)
    _wait_until_settled(qtbot, page)
    qtbot.waitUntil(lambda: page.import_button.isEnabled(), timeout=10000)

    job_id = page.import_paths(('C:/models/chair.fbx', 'C:/models/table.obj'))

    assert job_id == 'job-import-1'
    assert service.calls == [('import_paths',
                              ('C:/models/chair.fbx', 'C:/models/table.obj'),
                              None)]
    status = page.status.text()
    assert '已提交' in status
    assert '等待后台完成' in status, '任务被接受不等于导入完成'
    assert '导入了' not in status and '导入完成' not in status


def test_export_uses_the_frozen_selection_and_reports_the_count(page, qtbot,
                                                               monkeypatch):
    service = FakeService(
        FakeRepository(3),
        ModelCapabilities(browse=True, export_originals=True))
    page.set_service(service)
    page.bind_scope('project-a', 1)
    _wait_until_settled(qtbot, page)
    page._selection_toggled('model-000', False)
    page._selection_toggled('model-002', True)

    snapshot = page.selection.freeze()
    assert isinstance(snapshot, SelectionSnapshot)
    dialog = page.export_originals(snapshot)
    assert service.calls == []
    dialog.fileSelected.emit('D:/导出')
    dialog.reject()

    # The user changes the selection while the export is in flight.
    page._selection_toggled('model-001', False)
    assert service.calls == [('export_originals',
                              ('model-000', 'model-002'), 'D:/导出')]
    assert '2 个模型原件' in page.status.text()


def test_export_without_a_selection_does_not_open_a_destination_dialog(
        page, qtbot, monkeypatch):
    called = []
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getExistingDirectory',
        staticmethod(lambda *args, **kwargs: called.append(True) or 'D:/导出'))
    service = FakeService(
        FakeRepository(1),
        ModelCapabilities(browse=True, export_originals=True))
    page.set_service(service)
    page.bind_scope('project-a', 1)
    _wait_until_settled(qtbot, page)

    page._export_selection()
    assert page.status.text() == '请先选择要导出的模型'
    assert called == []
    assert service.calls == []


def test_a_capability_without_a_handler_keeps_the_button_disabled(page):

    class PartialService:
        capabilities = ModelCapabilities(
            browse=True, import_models=True, export_originals=True,
            edit_appearance=True, preview=True)

        def query(self, query):
            return ModelPage((), 0)

    page.set_service(PartialService())
    page.bind_scope('project-a', 1, refresh=False)
    for button in (page.import_button, page.folder_button, page.export_button,
                   page.details.open_button, page.details.apply_button):
        assert not button.isEnabled()


def test_a_capability_without_a_project_session_is_not_enough(page, qtbot):
    service = FakeService(
        FakeRepository(1),
        ModelCapabilities(browse=True, import_models=True))
    page.set_service(service)
    _wait_until_settled(qtbot, page)

    assert not page.import_button.isEnabled(), '没有项目会话就不能写入'
    assert page.import_paths(('C:/models/chair.fbx',)) is None
    assert service.calls == []


# ----------------------------------------------------------------------
# C3/C4: job feedback
# ----------------------------------------------------------------------
def test_a_job_is_never_reported_finished_before_it_is(page, qtbot):
    _load(page, qtbot, FakeRepository(1))
    page.publish_job_event({'job_id': 'j1', 'kind': '导入', 'state': 'queued'})
    assert '进行中' in page.status.text()
    assert '完成' not in page.status.text()

    page.publish_job_event({'job_id': 'j1', 'kind': '导入', 'state': 'running'})
    assert '进行中' in page.status.text()
    assert '完成' not in page.status.text()


def test_a_partial_export_is_reported_as_partial(page, qtbot):
    _load(page, qtbot, FakeRepository(1))
    page.publish_job_event({'job_id': 'j2', 'kind': '导出原件', 'state': 'completed',
                            'missing': ['a', 'b']})
    assert '部分完成' in page.status.text()
    assert '2 项缺失' in page.status.text()
    assert '（j2）' in page.status.text()


def test_a_failed_job_shows_the_error_and_the_job_id(page, qtbot):
    _load(page, qtbot, FakeRepository(1))
    page.publish_job_event({'job_id': 'j3', 'kind': '导入', 'state': 'failed',
                            'error_message': '磁盘已满', 'error_code': 'E_DISK'})
    status = page.status.text()
    assert '失败' in status and '磁盘已满' in status and 'E_DISK' in status
    assert 'j3' in status


def test_cancelled_and_stale_jobs_do_not_read_as_success(page, qtbot):
    _load(page, qtbot, FakeRepository(1))
    page.publish_job_event({'job_id': 'j4', 'kind': '导入', 'state': 'cancelled'})
    assert '已取消' in page.status.text()

    page.publish_job_event({'job_id': 'j5', 'kind': '导入', 'state': 'stale'})
    assert '已过期' in page.status.text()
    assert '完成' not in page.status.text()


# ----------------------------------------------------------------------
# C4: one appearance revision per user action
# ----------------------------------------------------------------------
def _open_ready_page(page, qtbot, count=2):
    """Install a page whose service really supports appearance editing."""
    service = FakeService(
        FakeRepository(count),
        ModelCapabilities(browse=True, edit_appearance=True))
    page.set_service(service)
    page.bind_scope('project-a', 1)
    _wait_until_settled(qtbot, page)
    page._selection_toggled('model-000', False)
    qtbot.waitUntil(lambda: page.details._record is not None and page.details._record.model_id == 'model-000')
    return service


def _saves(service):
    return [call for call in service.calls if call[0] == 'update_appearance']


def test_a_run_of_appearance_edits_is_saved_once(page, qtbot):
    service = _open_ready_page(page, qtbot)
    assert page.details._capabilities.edit_appearance

    # A slider drag: twelve patches through the existing slot control.
    for step in range(12):
        page.details._slot_options('basecolor', {'value': 0.2 + step * 0.02})

    assert _saves(service) == [], '拖动过程中不落库'
    assert page.details.preview_status.text() == '封面更新中'

    qtbot.waitUntil(lambda: bool(_saves(service)), timeout=10000)
    saves = _saves(service)
    assert len(saves) == 1, '一次连续编辑只提交一次保存'
    assert saves[0][1] == 'model-000'
    appearance = saves[0][2]
    assert appearance.revision == 4, '一次用户动作只涨一个外观修订'
    # The merged snapshot carries the last value of the drag, not the first.
    from prism.model_library import appearance as adapter
    assert adapter.slot_state(appearance, 'basecolor')['value'] == pytest.approx(0.42)


def test_switching_model_submits_the_pending_edit_first(page, qtbot):
    service = _open_ready_page(page, qtbot)

    page.details._slot_options('roughness', {'value': 0.3})
    page._selection_toggled('model-001', False)

    saves = _saves(service)
    assert len(saves) == 1
    assert saves[0][1] == 'model-000', '切换选择前必须先提交上一个模型的编辑'


def test_a_project_switch_drops_an_unsubmitted_edit(page, qtbot):
    service = _open_ready_page(page, qtbot)

    page.details._slot_options('ao', {'value': 0.5})
    page.bind_scope('project-b', 2, refresh=False)
    qtbot.wait(600)  # longer than the coalescing window

    assert _saves(service) == [], '切项目后旧项目的外观编辑不能被提交到新项目'
