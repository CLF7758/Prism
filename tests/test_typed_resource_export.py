"""Typed left-tree exports: target identity, original bytes and atomic output."""
from types import SimpleNamespace
import json
import zipfile
from pathlib import Path

import pytest

from prism.fileio.resource_export import (ResourceSubtreeExporter, ResourceExportError,
    ExportEntry, ExportPlan, write_new_directory, write_single_entry, write_zip)
from prism.fileio.resource_export_preparation import PageBodyReader
from prism.workspace_service import ResourceTreeService
from tests.test_partial_project import project, png
from tests.test_ai10_c6_resource_export_body import (FakeView, SyncWorker, standard_project,
    deferred_state, fake_scene, DOC_PAGE, SECOND_DOC_PAGE, DOC_MARK, SECOND_DOC_HTML,
    DOC_HTML, tree_service, docx_text)


class Original:
    TYPE = 'pixmap'
    filename = 'original.png'
    save_id = 1
    _prism_source = _source_path = None
    def __init__(self, payload):
        self._source_blob = payload
    def original_format(self):
        return 'png'
    def original_bytes(self):
        pytest.fail('Planning must not read original bytes')
    def pixmap_to_bytes(self):
        pytest.fail('Original mode must never re-encode a preview')


def canvas_export(items, mode='original_images'):
    service = ResourceTreeService('project')
    node = service.createPage('canvas', 'canvas', 'canvas')
    exporter = ResourceSubtreeExporter(service, asset_mode=mode, canvas_items=lambda page_id: items)
    return exporter.build_plan('canvas', node['id'])


def test_original_snapshot_never_decodes_and_is_not_changed_by_later_edit(tmp_path):
    image = Original(b'original-preserved')
    plan = canvas_export([image])
    image._source_blob = b'later-edit'
    root = Path(write_new_directory(plan, tmp_path))
    assert (root / 'original.png').read_bytes() == b'original-preserved'
    assert plan.lossless_assets == 1 and plan.reencoded_assets == 0


def test_missing_original_is_rejected_instead_of_using_preview():
    with pytest.raises(ResourceExportError, match='没有可用原件'):
        canvas_export([Original(None)])


def test_image_only_reports_excluded_video_without_mislabelling_it():
    video = SimpleNamespace(TYPE='video', filename='clip.mp4', _video_blob=b'video')
    plan = canvas_export([Original(b'image'), video])
    assert len(plan.entries) == 1 and any('clip.mp4' in note for note in plan.notes)
    plan = canvas_export([Original(b'image'), video], 'original_all')
    assert len(plan.entries) == 2 and plan.lossless_assets == 2


def test_archived_original_is_read_only_when_written(project, png, tmp_path):
    image = Original(None)
    image._prism_source = (str(project), 12)
    plan = canvas_export([image])
    root = Path(write_new_directory(plan, tmp_path))
    assert (root / 'original.png').read_bytes() == png


def test_mindmap_body_snapshot_is_deep_not_a_live_nested_alias():
    tree = {'data': {'text': 'before'}, 'children': [{'data': {'text': 'child'}}]}
    reader = PageBodyReader([dict(id='map', kind='mindmap', content=tree)])
    tree['children'][0]['data']['text'] = 'after'
    assert reader.read('map').content['children'][0]['data']['text'] == 'child'


def test_failed_subtree_does_not_publish_partial_folder_or_zip(tmp_path):
    def fail(path):
        raise OSError('injected writer failure')
    plan = ExportPlan('root', (ExportEntry('one.bin', payload=b'one'), ExportEntry('two.bin', writer=fail)))
    write_new_directory(plan, tmp_path)
    assert not (tmp_path / 'root').exists()
    assert not list(tmp_path.glob('.prism-export-*'))
    old = tmp_path / 'existing.zip'
    old.write_bytes(b'previous-output')
    write_zip(plan, old)
    assert old.read_bytes() == b'previous-output'


def test_late_cancel_of_single_file_keeps_previous_target(tmp_path):
    worker = SyncWorker(lambda: None)
    def generated(path):
        Path(path).write_bytes(b'new-data')
        worker.canceled = True
    target = tmp_path / 'previous.docx'
    target.write_bytes(b'original-output')
    write_single_entry(ExportPlan('document', (ExportEntry('doc.docx', writer=generated),)), target, worker)
    assert target.read_bytes() == b'original-output'


@pytest.mark.parametrize('extension', ['docx', 'pdf'])
def test_right_clicked_unloaded_document_is_target_not_active_page(qapp, tmp_path, monkeypatch, extension):
    from prism.actions import export_workflow
    from prism.fileio import document_export
    from PyQt6 import QtWidgets
    source = standard_project(tmp_path)
    state = deferred_state(source)
    service = tree_service([(DOC_PAGE, 'document', 'first', DOC_HTML),
                            (SECOND_DOC_PAGE, 'document', 'second', SECOND_DOC_HTML)])
    node = next(node for node in service.nodes if node.get('pageId') == SECOND_DOC_PAGE)
    scene = fake_scene(state)
    scene.project_id = 'project'
    view = FakeView(scene, service, str(source))
    view._trace_open_epoch = 1
    target = tmp_path / ('selected.'+extension)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw:
        (str(target), 'PDF (*.pdf)' if extension == 'pdf' else 'Word (*.docx)'))
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', lambda *a: None)
    monkeypatch.setattr(export_workflow.widgets, 'PrismProgressDialog', lambda *a, **kw: None)
    monkeypatch.setattr(export_workflow.fileio, 'ThreadedIO', SyncWorker)
    seen = []
    writer_name = 'export_note_to_pdf' if extension == 'pdf' else 'export_note_to_word'
    writer = getattr(document_export, writer_name)
    def capture(html, attachments, path):
        seen.append(html)
        return writer(html, attachments, path)
    monkeypatch.setattr(document_export, writer_name, capture)
    export_workflow.export_resource_subtree(view, 'document', node['id'], typed=True)
    assert target.is_file() and not view.reported
    assert seen == [SECOND_DOC_HTML]
    if extension == 'pdf':
        assert target.read_bytes().startswith(b'%PDF')
    else:
        assert DOC_MARK not in docx_text(target)


def test_dialog_cannot_redirect_old_export_to_a_new_project(tmp_path, monkeypatch):
    from prism.actions import export_workflow
    from PyQt6 import QtWidgets
    source = standard_project(tmp_path)
    service = tree_service([(DOC_PAGE, 'document', 'doc', DOC_HTML)])
    node = next(node for node in service.nodes if node.get('pageId') == DOC_PAGE)
    scene = fake_scene(deferred_state(source))
    scene.project_id = 'first-project'
    view = FakeView(scene, service, str(source))
    target = tmp_path / 'must-not-write.docx'
    def changed(*args, **kwargs):
        scene.project_id = 'different-project'
        return str(target), 'Word (*.docx)'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', changed)
    notes = []
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', lambda view, message: notes.append(message))
    export_workflow.export_resource_subtree(view, 'document', node['id'], typed=True)
    assert not target.exists() and view.worker is None
    assert any('已变化' in note for note in notes)


def test_unloaded_all_media_scope_queries_each_kind_and_streams_originals(project, png, tmp_path):
    from prism.actions.export_workflow import _catalog_page_assets
    calls = []
    kinds = {'pixmap': (1, 'one.png'), 'video': (2, 'two.mp4'), 'glb': (3, 'three.glb')}
    class Scope:
        project_path = str(project)
        def iter_query(self, *, kind, canvas_ids, **kwargs):
            calls.append((kind, canvas_ids))
            return iter([{'id': kinds[kind][0]}])
        def details(self, ids, **kwargs):
            return [dict(id=key, data=json.dumps({'filename': next(name for number, name in kinds.values() if number == key)}))
                    for key in ids]
    items = _catalog_page_assets(Scope(), 'unloaded-B', all_media=True)
    plan = canvas_export(items, 'original_all')
    root = Path(write_new_directory(plan, tmp_path))
    assert calls == [(kind, ['unloaded-B']) for kind in kinds]
    assert {file.name for file in root.iterdir()} == {name for _, name in kinds.values()}
    assert all(file.read_bytes() == png for file in root.iterdir())


def test_formal_resource_dispatch_opts_into_typed_exports(main_window, monkeypatch):
    from prism.actions import export_workflow
    calls = []
    monkeypatch.setattr(export_workflow, 'export_resource_subtree', lambda *args, **kwargs: calls.append((args, kwargs)))
    main_window._export_resource_item('document', 'right-clicked-node')
    assert calls == [((main_window.view, 'document', 'right-clicked-node'), {'typed': True})]


def test_default_canvas_context_menu_can_export_without_materializing_a_node(main_window, qtbot, monkeypatch):
    from prism.widgets.resource_tree_model import NODE_ID_ROLE
    from prism.actions import export_workflow
    # Keep the real signal chain, but this menu-only probe must not open a
    # native destination dialog through the main-window subscriber.
    monkeypatch.setattr(export_workflow, 'export_resource_subtree', lambda *args, **kwargs: None)
    tree = main_window.view.category_panel.resource_tree
    index = tree.model().index_for_id('implicit:default-canvas')
    assert index.isValid()
    seen = []
    tree.export_requested.connect(lambda section, node_id: seen.append((section, node_id)))
    menu = tree.menu_for_index(index)
    assert tree.currentIndex() == index
    exports = [action for action in menu.actions() if '导出' in action.text() or 'Export' in action.text()]
    assert len(exports) == 1
    exports[0].trigger()
    assert seen == [('canvas', index.data(NODE_ID_ROLE))]
    service = tree.model().service
    plan = ResourceSubtreeExporter(service, asset_mode='original_images',
        canvas_items=lambda page_id: [Original(b'original')]).build_plan('canvas', 'implicit:default-canvas')
    assert len(plan.entries) == 1 and service._by_id('implicit:default-canvas') is None
    menu.deleteLater()


def test_deferred_body_refuses_source_changed_after_plan(tmp_path):
    import os
    source = standard_project(tmp_path)
    reader = PageBodyReader([{'id': DOC_PAGE, 'kind': 'document'}], source=source)
    stamp = source.stat()
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000000))
    result = reader.read(DOC_PAGE)
    assert not result.resolved and '已变化' in result.reason


def test_formal_sidebar_document_pdf_runs_real_worker(main_window, qtbot, tmp_path, monkeypatch):
    from PyQt6 import QtWidgets
    from prism.actions import export_workflow
    service = main_window.view.category_panel.resource_service
    node = service.createPage('document', '指定文档', 'typed-doc')
    main_window.view.scene.workspace_pages.append(dict(id='typed-doc', kind='document',
        title='指定文档', content='<p>完整正文：中文 PDF</p>', tags=[]))
    target = tmp_path / 'typed.pdf'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(target), 'PDF (*.pdf)'))
    main_window._export_resource_item('document', node['id'])
    worker = main_window.view.worker
    assert worker is not None
    qtbot.waitUntil(lambda: main_window.view.worker is None, timeout=10000)
    assert worker.args == () and worker.kwargs == {} and worker.func is None
    assert target.read_bytes().startswith(b'%PDF')
    assert not list(tmp_path.glob('*.partial'))


def test_unloaded_mindmap_uses_xmind_and_offers_the_pdf_export(tmp_path, monkeypatch):
    """没打开过的脑图页也能导出：两种出口都从工程文件里的树出发。

    PDF 那条是纯 Python 画的整页矢量图（prism/fileio/mindmap_pdf.py），
    不需要页面被打开过 —— 所以对话框里同时给 XMind 和 PDF。
    """
    from tests.test_ai10_c6_resource_export_body import MINDMAP_PAGE, MINDMAP_MARK, MINDMAP_TREE
    from prism.actions import export_workflow
    from PyQt6 import QtWidgets
    source = standard_project(tmp_path)
    service = tree_service([(MINDMAP_PAGE, 'mindmap', 'map', MINDMAP_TREE)])
    node = next(node for node in service.nodes if node.get('pageId') == MINDMAP_PAGE)
    scene = fake_scene(deferred_state(source))
    view = FakeView(scene, service, str(source))
    target = tmp_path / 'requested.xmind'
    def choose(*args):
        assert 'XMind (*.xmind)' in args[-1], args[-1]
        assert 'PDF' in args[-1], args[-1]
        return str(target), 'XMind (*.xmind)'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', choose)
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification', lambda *a: None)
    monkeypatch.setattr(export_workflow.widgets, 'PrismProgressDialog', lambda *a, **kw: None)
    monkeypatch.setattr(export_workflow.fileio, 'ThreadedIO', SyncWorker)
    export_workflow.export_resource_subtree(view, 'mindmap', node['id'], typed=True)
    assert not view.reported
    with zipfile.ZipFile(target) as archive:
        assert MINDMAP_MARK in archive.read('content.json').decode('utf-8')


@pytest.mark.parametrize('relative', ['../escape', '/absolute', 'C:/escape', r'a\..\escape', 'name:stream'])
def test_plan_rejects_paths_outside_requested_destination(relative):
    with pytest.raises(ResourceExportError, match='不安全'):
        ExportEntry(relative, payload=b'original')


def test_malformed_original_format_cannot_escape_export_root():
    item = Original(b'original')
    item.original_format = lambda: '../../escape'
    with pytest.raises(ResourceExportError, match='不安全'):
        canvas_export([item])
