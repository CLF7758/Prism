"""C1/C5/C6: the model page inside the existing window, tree and gate.

Nothing here re-implements navigation. The checks are about *reuse*:

* the model entry goes through the existing commit gate and the existing
  navigation path, and leaves the material pages untouched;
* the model section of the resource tree creates nothing and accepts no drop
  until the model domain can actually save it;
* a project switch invalidates the page, the record and the covers of the
  previous project.
"""
import pytest
from PyQt6 import QtCore
from PyQt6.QtCore import Qt

from prism.model_library.contracts import (
    ModelCardSummary, ModelPage, ModelQuery, ModelRecord,
)
from prism.model_library.service import ModelLibraryService
from prism.widgets.model_library import ModelLibraryState
from prism.widgets.resource_tree_model import (
    ASSET_COUNT_ROLE, UI_SECTIONS, ResourceTreeModel)
from prism.widgets.resource_tree_view import ResourceTreeView
from prism.workspace_service import ResourceTreeService, TREE_SECTIONS


class FakeRepository:
    def __init__(self, count=3):
        self.count = count
        self.queries = []

    def _names(self, text):
        names = ['model-%03d' % index for index in range(self.count)]
        return [name for name in names if not text or text in name]

    def query(self, query):
        self.queries.append(query)
        names = self._names(query.text)
        page = names[query.offset:query.offset + query.limit]
        return ModelPage(tuple(ModelCardSummary(name, name, 'fbx') for name in page),
                         len(names), query.offset)

    def get(self, model_id):
        return ModelRecord(model_id=model_id, project_id='project-a',
                           name=model_id, main_file_id='file-1', format='fbx',
                           source_revision=5, record_revision=5)

    def save(self, record, *, expected_revision):
        raise AssertionError('模型库页面不得直接写仓库')


def make_tree(service=None):
    service = service or ResourceTreeService('project')
    return service, ResourceTreeModel(service)


def model_section_index(model):
    return model.index(UI_SECTIONS.index('model'), 0)


# ----------------------------------------------------------------------
# The model section stays closed until the domain can save it
# ----------------------------------------------------------------------
def test_the_model_section_is_ui_only_by_default(qapp):
    service, model = make_tree()
    index = model_section_index(model)

    assert 'model' not in TREE_SECTIONS
    assert UI_SECTIONS[UI_SECTIONS.index('material') + 1] == 'model'
    assert not service.model_section_enabled
    assert not model.model_section_enabled()
    assert not (index.flags() & Qt.ItemFlag.ItemIsDropEnabled), (
        '模型分区在模型域保存被验收之前不能接收拖放')
    with pytest.raises(ValueError):
        service.createFolder('model', '角色')


def test_the_composition_root_can_open_the_model_section(qapp):
    service, model = make_tree()
    assert service.enable_model_section() is True

    index = model_section_index(model)
    assert model.model_section_enabled()
    assert index.flags() & Qt.ItemFlag.ItemIsDropEnabled
    folder = service.createFolder('model', '角色')
    assert folder['section'] == 'model'
    assert folder['nodeType'] == 'folder'
    # A model folder is a folder, never a canvas page.
    with pytest.raises(ValueError):
        service.createPage('model', '不是画布页', 'page-1')


def test_the_add_button_is_not_painted_on_a_closed_model_section(qtbot):
    """The delegate asks the tree it belongs to, not a detached model."""
    service, model = make_tree()
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    delegate = view.itemDelegate()
    rect = QtCore.QRect(0, 0, 200, 32)
    index = model_section_index(model)

    assert delegate.add_button_rect(rect, index) is None
    # The material section keeps its button; the model gate is not global.
    assert delegate.add_button_rect(
        rect, model.index(UI_SECTIONS.index('material'), 0)) is not None

    service.enable_model_section()
    assert delegate.add_button_rect(rect, index) is not None


def test_a_closed_model_section_offers_no_creation_menu(qapp):
    service, model = make_tree()
    view = ResourceTreeView()
    index = model_section_index(model)
    view.setModel(model)

    actions = [action.text() for action in view.menu_for_index(index).actions()]
    assert '打开模型库' in actions
    assert not any('新建' in action for action in actions)
    assert any('尚未接入' in action for action in actions)

    service.enable_model_section()
    actions = [action.text() for action in view.menu_for_index(index).actions()]
    assert '新建文件夹' in actions


def test_the_section_row_counts_models_and_not_their_files(qapp):
    service, model = make_tree()
    index = model_section_index(model)
    assert index.data(ASSET_COUNT_ROLE) is None, '未知不能显示成 0'

    model.set_model_count(12)
    assert index.data(ASSET_COUNT_ROLE) == 12

    model.set_model_count(None)
    assert index.data(ASSET_COUNT_ROLE) is None


# ----------------------------------------------------------------------
# Navigation
# ----------------------------------------------------------------------
def test_the_model_entry_uses_the_existing_commit_gate(main_window, qtbot):
    requested = []
    original = main_window.request_navigation
    main_window.request_navigation = lambda callback: (
        requested.append(callback), original(callback))[1]

    main_window.open_model_library()
    qtbot.waitUntil(
        lambda: main_window.tabs.currentWidget() is main_window.model_library,
        timeout=20000)

    assert requested, '模型入口必须走既有导航提交门，不能直接切页'
    assert main_window.current_page_kind == 'model'
    assert main_window.model_library.details.isHidden() is False
    assert main_window.material_library.details.isHidden() is True


def test_the_page_binds_the_project_session_and_the_open_epoch(main_window, qtbot):
    view = main_window.view
    main_window.open_model_library()
    qtbot.waitUntil(
        lambda: main_window.tabs.currentWidget() is main_window.model_library,
        timeout=20000)

    library = main_window.model_library
    project_id, epoch = library._host_scope()
    assert library.scope_token() == '%s#%d' % (project_id, epoch)
    assert epoch == int(getattr(view, '_trace_open_epoch', 0) or 0)


def test_a_project_switch_drops_the_old_page_record_and_selection(
        main_window, qtbot):
    view = main_window.view
    main_window.open_model_library()
    qtbot.waitUntil(
        lambda: main_window.tabs.currentWidget() is main_window.model_library,
        timeout=20000)

    library = main_window.model_library
    library.set_service(ModelLibraryService(FakeRepository(2)))
    library.bind_scope(*library._host_scope())
    qtbot.waitUntil(lambda: library.state is not ModelLibraryState.LOADING,
                    timeout=20000)
    library._selection_toggled('model-000', False)
    assert library.details.info['revision'].text() == '5'

    stale_identity = library._identity
    stale_page = FakeRepository(2).query(ModelQuery(offset=0, limit=64))
    old_token = library.scope_token()

    # The project switch happens without the model tab being left.
    view._trace_open_epoch = int(getattr(view, '_trace_open_epoch', 0) or 0) + 1
    view.project_open_finished.emit(object())

    assert library.scope_token() != old_token
    assert library.scope_token() == '%s#%d' % (
        view.scene.project_id, view._trace_open_epoch)
    assert library.selection.count == 0, '旧项目的选择不能带进新项目'
    assert library._summaries == {}
    assert library._pending_pages == set(), '切项目后不偷偷在后台跑查询'
    assert library.details.info['format'].text() == '—', '旧项目的记录必须清掉'
    assert library.publish_page(stale_page, stale_identity) is False, (
        '旧项目的封面/摘要不能提交到新项目')


def test_returning_to_the_model_page_does_not_query_twice(main_window, qtbot):
    main_window.open_model_library()
    qtbot.waitUntil(
        lambda: main_window.tabs.currentWidget() is main_window.model_library,
        timeout=20000)

    library = main_window.model_library
    repository = FakeRepository(2)
    library.set_service(ModelLibraryService(repository))
    library.bind_scope(*library._host_scope())
    qtbot.waitUntil(lambda: library.state is ModelLibraryState.READY,
                    timeout=20000)
    qtbot.waitUntil(lambda: len(repository.queries) == 1, timeout=20000)

    library.load_from_scene()
    qtbot.wait(50)
    assert len(repository.queries) == 1, (
        '重新进入已经就绪的模型页不应再发一次同样的查询')
