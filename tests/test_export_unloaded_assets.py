"""未加载素材的原件导出：按需打开的工程里也能导出整库 / 浏览器选中项。

这一组测试盯住三件事，任何一条都不能退化：

1. **取项**来自目录（`ProjectOpenScope.iter_query()` 分页 + `details()`
   分批），不是只取 scene 里已加载的那一页。
2. **原件字节**必须逐字节等于工程归档里存的那份 —— 断言的是字节相等，
   不是「文件存在」。素材没加载，没有解码结果可以顶替，所以字节对不上
   就一定是走了别的路（预览重编码 / 空文件）。
3. **诚实**：目录不可用就明确拒绝；缺原件的条目报错而不是写一个空文件；
   取消要真的停下；同名要自动避让而不是逐个弹窗。
"""
from collections import namedtuple
from contextlib import closing
import json
import sqlite3

import pytest

from prism.actions import export_workflow
from prism.export_service import (ExportService, ExportSourceMissing,
                                  ExportTarget, OnDiskExportSource,
                                  catalog_ids)
from prism.fileio.export import ImagesToDirectoryExporter
from prism.image_source import ImageSourceMixin
from prism.project_open_scope import ProjectOpenScope


Asset = namedtuple('Asset', 'id canvas filename payload')

CANVAS_A = 'canvas-a'
CANVAS_B = 'canvas-b'

#: A real PNG signature followed by bytes a re-encode can never reproduce.
#: If the exporter decodes and re-encodes (or writes a preview), the
#: assertion `written == payload` fails.
def original_bytes(index, size=64):
    return (b'\x89PNG\r\n\x1a\n' + bytes([index % 256]) * size +
            f'-original-{index}'.encode('ascii'))


def assets(count=6, start=1, canvas=CANVAS_A):
    return [Asset(start+index, canvas, f'asset-{start+index}.png',
                  original_bytes(start+index))
            for index in range(count)]


def build_project(path, rows):
    """A .prism with one `items` row and one `sqlar` original per asset.

    ``payload=None`` means the project genuinely has no original for that
    asset - the case that must be reported rather than faked.
    """
    with closing(sqlite3.connect(str(path))) as connection:
        connection.executescript('''
            CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);
            CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,name TEXT,mode INT,mtime INT,sz INT,data BLOB);
            CREATE TABLE workspace_pages(id TEXT,kind TEXT,title TEXT);
        ''')
        for canvas in sorted({row.canvas for row in rows}):
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?)',
                               (canvas, 'canvas', canvas))
        for row in rows:
            extra = dict(canvasId=row.canvas, title=f'Asset {row.id}',
                         filename=row.filename, sourceFormat='png',
                         imageWidth=8, imageHeight=8, tags=[])
            connection.execute(
                'INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                (row.id, 'pixmap', row.id, 0, 0, 1, 0, 1, json.dumps(extra)))
            if row.payload is not None:
                connection.execute(
                    'INSERT INTO sqlar(item_id,name,mode,mtime,sz,data) '
                    'VALUES(?,?,?,?,?,?)',
                    (row.id, f'{row.id:04}-{row.filename}', 0o644, 0,
                     len(row.payload), row.payload))
        connection.commit()
    return path


class LoadedAsset(ImageSourceMixin):
    """One asset that is in the scene, with an in-memory original."""

    TYPE = 'pixmap'

    def __init__(self, save_id, filename, payload, canvas=CANVAS_A):
        self.save_id = save_id
        self.filename = filename
        self._source_blob = payload
        self._source_path = None
        self._prism_source = None
        self._source_format = 'png'
        self._deleted_at = None
        self._canvas_id = canvas


class FakeScene:
    """Only the handful of methods the export path actually asks for."""

    def __init__(self, project, loaded=(), selected=()):
        self._partial_project_source = str(project)
        self.workspace_pages = []
        self.attachments = {}
        self._loaded = list(loaded)
        self._selected = list(selected)

    def items_for_save(self):
        return list(self._loaded)

    def selectedItems(self, user_only=False):        # noqa: N802
        return list(self._selected)

    def items_by_type(self, kind):
        return [item for item in self._loaded
                if getattr(item, 'TYPE', None) == kind]


class FakeSignal:
    def __init__(self, sink=None):
        self.calls = []
        self.slots = []
        self._sink = sink

    def emit(self, *args):
        self.calls.append(args)
        if self._sink is not None:
            self._sink(args)
        for slot in self.slots:
            slot(*args)

    def connect(self, slot):
        self.slots.append(slot)


class FakeWorker:
    """Runs the export synchronously, optionally cancelling mid-run."""

    def __init__(self, func, cancel_after=None):
        self.func = func
        self.args = ()
        self.kwargs = {}
        self.canceled = False
        self.cancel_after = cancel_after
        self.progress = FakeSignal(self._on_progress)
        self.finished = FakeSignal()
        self.begin_processing = FakeSignal()
        self.user_input_required = FakeSignal()

    def _on_progress(self, args):
        if self.cancel_after is not None and args and args[0] >= self.cancel_after:
            self.canceled = True

    def start(self):
        self.func(*self.args, **self.kwargs, worker=self)


class FakeView:
    def __init__(self, scene, scope, mode='original', window=None):
        self.scene = scene
        self._open_scope = scope
        self.export_service = ExportService(scene)
        self.filename = None
        self.worker = None
        self.exporter = None
        self.progress = None
        self.finished = []
        self.asked_about = []
        self._mode = mode
        self._window = window

    def _ask_export_mode(self):
        return self._mode

    def window(self):
        return self._window

    def on_export_finished(self, filename, errors):
        self.finished.append((filename, errors))

    def on_export_images_file_exists(self, filename):
        # Reaching this would mean the bulk path popped a per-file dialog.
        self.asked_about.append(filename)


@pytest.fixture
def notifications(monkeypatch):
    shown = []
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification',
                        lambda parent, text, *a, **k: shown.append(text))
    monkeypatch.setattr(export_workflow.widgets, 'PrismProgressDialog',
                        lambda *a, **k: None)
    return shown


@pytest.fixture
def run_export(monkeypatch, tmp_path):
    """Drive `export_images()` into `tmp_path` with a synchronous worker."""
    workers = []

    def factory(func, *args, **kwargs):
        worker = FakeWorker(func)
        worker.args = args
        worker.kwargs = kwargs
        workers.append(worker)
        return worker

    monkeypatch.setattr(export_workflow.fileio, 'ThreadedIO', factory)

    def run(view, outdir=None, cancel_after=None, selected_only=False):
        outdir = outdir or (tmp_path/'out')
        outdir.mkdir(parents=True, exist_ok=True)
        from PyQt6 import QtWidgets
        monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory',
                            lambda **kwargs: str(outdir))
        if cancel_after is not None:
            def cancelling(func, *args, **kwargs):
                worker = FakeWorker(func, cancel_after=cancel_after)
                worker.args, worker.kwargs = args, kwargs
                workers.append(worker)
                return worker
            monkeypatch.setattr(export_workflow.fileio, 'ThreadedIO',
                                cancelling)
        export_workflow.export_images(view, selected_only)
        return workers[-1] if workers else None

    return run


@pytest.fixture
def project(tmp_path):
    """Two canvases, six originals, one without any original bytes."""
    rows = (assets(count=3, start=1, canvas=CANVAS_A) +
            assets(count=3, start=4, canvas=CANVAS_B) +
            [Asset(7, CANVAS_B, 'asset-7.png', None)])
    path = build_project(tmp_path/'partial.prism', rows)
    scope = ProjectOpenScope(path, tmp_path/'catalog.sqlite')
    assert scope.build(batch_size=64).complete
    return path, scope, rows


def exported_files(directory):
    return sorted(path.name for path in directory.iterdir() if path.is_file())


# ── 取项：整库导出覆盖未加载的素材 ────────────────────────────


def test_whole_project_export_writes_unloaded_originals(project, tmp_path,
                                                        notifications, run_export):
    """`导出图像`（整库）必须覆盖没加载的那 5 个，而不是只导已加载的 1 个。"""
    path, scope, rows = project
    # 1 号素材已加载，而且**带着没保存的编辑**：它自己的字节和磁盘上那行
    # 不同。已加载的对象必须赢。
    edited = original_bytes(999)
    loaded = LoadedAsset(1, 'asset-1.png', edited)
    view = FakeView(FakeScene(path, [loaded]), scope)

    run_export(view)

    outdir = tmp_path/'out'
    assert exported_files(outdir) == [
        f'{row.id:04d}-{row.filename}' for row in rows if row.payload is not None]
    for row in rows:
        if row.payload is None or row.id == 1:
            continue
        written = (outdir/f'{row.id:04d}-{row.filename}').read_bytes()
        assert written == row.payload, (
            f'{row.id:04d} 导出的不是工程里存着的原件: '
            f'{len(written)} 字节 vs {len(row.payload)} 字节')
    assert (outdir/'0001-asset-1.png').read_bytes() == edited, (
        '未保存的编辑不能被磁盘上旧的那一行盖掉')
    assert view.finished and view.finished[-1][1], (
        '没有原件的那一条必须报错，不能当成成功')
    assert not notifications


def test_unloaded_export_never_reads_a_whole_original(project, tmp_path,
                                                     notifications, run_export,
                                                     monkeypatch):
    """批量导出走的是分块流式读取，不是把原件整个读进内存。"""
    from prism import source_store

    def explode(*args, **kwargs):
        raise AssertionError('the export must stream, not read whole blobs')

    monkeypatch.setattr(source_store, 'read_archive', explode)
    view = FakeView(FakeScene(project[0], []), project[1])

    run_export(view)

    written = exported_files(tmp_path/'out')
    assert len(written) == 6, '每个有原件的素材都要写出来'


def test_directory_batches_stay_bounded_and_cancellable(project, tmp_path,
                                                        notifications,
                                                        run_export, monkeypatch):
    """`details()` 每批不超过目录上限，且一个大批次不会全量展开。"""
    path, scope, _rows = project
    batches = []
    original_details = ProjectOpenScope.details

    def recording(self, item_ids, **kwargs):
        batches.append(len(item_ids))
        return original_details(self, item_ids, **kwargs)

    monkeypatch.setattr(ProjectOpenScope, 'details', recording)
    view = FakeView(FakeScene(path, []), scope)

    run_export(view)

    assert batches, '导出必须真的按批去目录里取行'
    assert max(batches) <= 1024, batches


# ── 浏览器选中 == 导出选中 ─────────────────────────────────────


def test_browser_selection_is_what_gets_exported(project, tmp_path,
                                                 notifications, run_export):
    path, scope, rows = project
    loaded = LoadedAsset(2, 'asset-2.png', rows[1].payload)
    proxy = type('CatalogProxy', (), {'_catalog_proxy': True,
                                      'save_id': 4})()
    browser = type('Browser', (), {
        'model': type('Model', (), {'items': [proxy, loaded]})(),
        'list': type('List', (), {'selectedIndexes': lambda _self: [
            type('Index', (), {'row': lambda _self, index=0: index})(),
            type('Index', (), {'row': lambda _self, index=1: index})()]})()})()
    window = type('Window', (), {
        'asset_browser': browser,
        'tabs': type('Tabs', (), {'currentWidget': lambda _self: browser})()})()
    # scene 的选中集里是另一张 —— 导出不能听它的。
    other = LoadedAsset(6, 'asset-6.png', rows[5].payload)
    view = FakeView(FakeScene(path, [loaded, other], [other]), scope,
                    window=window)

    run_export(view, selected_only=True)

    assert exported_files(tmp_path/'out') == ['0002-asset-2.png',
                                              '0004-asset-4.png']
    assert (tmp_path/'out'/'0004-asset-4.png').read_bytes() == rows[3].payload


def test_selection_of_only_unloaded_assets_leaves_the_loaded_page_alone(
        project, tmp_path, notifications, run_export):
    """选中的全是未加载素材时，scene 里那一页不能被顺手带出去。"""
    path, scope, rows = project
    on_screen = LoadedAsset(1, 'asset-1.png', rows[0].payload)
    proxy = type('CatalogProxy', (), {'_catalog_proxy': True,
                                      'save_id': 4})()
    browser = type('Browser', (), {
        'model': type('Model', (), {'items': [proxy, on_screen]})(),
        'list': type('List', (), {'selectedIndexes': lambda _self: [
            type('Index', (), {'row': lambda _self: 0})()]})()})()
    window = type('Window', (), {
        'asset_browser': browser,
        'tabs': type('Tabs', (), {'currentWidget': lambda _self: browser})()})()
    view = FakeView(FakeScene(path, [on_screen]), scope, window=window)

    run_export(view, selected_only=True)

    assert exported_files(tmp_path/'out') == ['0004-asset-4.png']
    assert (tmp_path/'out'/'0004-asset-4.png').read_bytes() == rows[3].payload


def test_scene_selection_on_a_canvas_tab_does_not_widen_to_the_whole_project(
        project, tmp_path, notifications, run_export):
    """画布页上的「导出选中」就是 scene 选中的那些，不能顺手导全库。"""
    path, scope, rows = project
    picked = LoadedAsset(5, 'asset-5.png', rows[4].payload)
    other = LoadedAsset(6, 'asset-6.png', rows[5].payload)
    view = FakeView(FakeScene(path, [picked, other], [picked]), scope)

    run_export(view, selected_only=True)

    assert exported_files(tmp_path/'out') == ['0005-asset-5.png']


# ── 诚实：目录不可用就明确拒绝 ─────────────────────────────────


def test_partial_export_refuses_when_the_directory_is_not_ready(
        project, tmp_path, notifications, run_export, monkeypatch):
    path, scope, _rows = project
    monkeypatch.setattr(ProjectOpenScope, 'ready', property(lambda self: False))
    view = FakeView(FakeScene(path, []), scope)

    worker = run_export(view)

    assert len(notifications) == 1
    assert 'directory' in notifications[0]
    assert worker is None, '目录不可用时不该启动导出线程'
    assert not (tmp_path/'out').exists() or not exported_files(tmp_path/'out')


def test_selection_that_the_directory_cannot_resolve_is_refused(
        project, tmp_path, notifications, run_export):
    path, scope, _rows = project
    proxy = type('CatalogProxy', (), {'_catalog_proxy': True,
                                      'save_id': 4242})()
    browser = type('Browser', (), {
        'model': type('Model', (), {'items': [proxy]})(),
        'list': type('List', (), {'selectedIndexes': lambda _self: [
            type('Index', (), {'row': lambda _self: 0})()]})()})()
    window = type('Window', (), {
        'asset_browser': browser,
        'tabs': type('Tabs', (), {'currentWidget': lambda _self: browser})()})()
    view = FakeView(FakeScene(path, []), scope, window=window)

    worker = run_export(view, selected_only=True)

    assert len(notifications) == 1
    assert '4242' in notifications[0]
    assert worker is None


# ── 同名避让与取消 ─────────────────────────────────────────────


def test_existing_files_are_avoided_without_asking(project, tmp_path,
                                                   notifications, run_export):
    path, scope, rows = project
    outdir = tmp_path/'out'
    loaded = LoadedAsset(1, 'asset-1.png', rows[0].payload)
    view = FakeView(FakeScene(path, [loaded]), scope)

    worker = run_export(view, outdir=outdir)
    worker = run_export(view, outdir=outdir)

    assert not worker.user_input_required.calls, '批量导出不该逐个弹窗'
    assert not view.asked_about
    assert (outdir/'0001-asset-1.png').read_bytes() == rows[0].payload
    assert (outdir/'0001-asset-1 (2).png').read_bytes() == rows[0].payload
    assert len(exported_files(outdir)) == len(
        [row for row in rows if row.payload is not None])*2


def test_cancelling_stops_the_export_and_leaves_no_partial_file(
        project, tmp_path, notifications, run_export):
    path, scope, rows = project
    view = FakeView(FakeScene(path, []), scope)

    worker = run_export(view, cancel_after=1)

    outdir = tmp_path/'out'
    written = exported_files(outdir)
    assert 0 < len(written) < 6, '取消必须真的停下来，而不是导完整库'
    assert all((outdir/name).stat().st_size > 0 for name in written), (
        '取消不能留下半个空文件')
    assert worker.finished.calls, '收尾信号必须照发'
    for row in rows:
        name = f'{row.id:04d}-{row.filename}'
        if name in written and row.payload is not None:
            assert (outdir/name).read_bytes() == row.payload


# ── 缺原件要报错，不是写个空文件 ───────────────────────────────


def test_missing_original_is_reported_not_replaced(project, tmp_path,
                                                   notifications, run_export):
    path, scope, rows = project
    view = FakeView(FakeScene(path, []), scope)

    plan = ExportService(view.scene).plan(
        ExportTarget.ORIGINAL, items=[],
        source=OnDiskExportSource(scope, catalog_ids(scope), archive=path))

    assert plan.lossless is False
    assert plan.warnings, '缺原件必须先问用户，不能当成全都能无损导出'
    assert any('7' in warning for warning in plan.warnings)

    run_export(view)

    outdir = tmp_path/'out'
    assert not (outdir/'0007-asset-7.png').exists()
    failures = [error for _name, errors in view.finished for error in errors]
    assert any('0007-asset-7.png' in error for error in failures)


def test_adjusted_export_says_it_cannot_touch_unloaded_assets(
        project, notifications):
    path, scope, _rows = project
    source = OnDiskExportSource(scope, catalog_ids(scope), archive=path)

    plan = ExportService(None).plan(ExportTarget.ADJUSTED, items=[],
                                    source=source)

    assert plan.reencodes is True and plan.lossless is False
    assert plan.warnings, '调整后导出做不到的事必须先说清楚'


# ── 已加载的永远优先于磁盘记录 ─────────────────────────────────


def test_loaded_ids_are_excluded_from_the_disk_source(project):
    _path, scope, rows = project
    loaded = LoadedAsset(3, 'asset-3.png', rows[2].payload)

    source = OnDiskExportSource(scope, catalog_ids(scope), loaded=[loaded])

    assert 3 not in source._pending
    assert source.total == len(rows)-1, '7 号没有原件，但仍然要被尝试导出'
    assert sorted(source._pending) == sorted(
        row.id for row in rows if row.id not in (3, ))


def test_exporter_totals_include_the_unloaded_assets(project, tmp_path):
    path, scope, rows = project
    source = OnDiskExportSource(scope, catalog_ids(scope), archive=path)

    exporter = ImagesToDirectoryExporter(
        FakeScene(path, []), str(tmp_path), adjusted=False, source=source,
        unique_names=True)

    assert exporter.num_total == len(rows)
    assert source.total == len(rows)


def test_unloaded_assets_have_no_preview_to_fall_back_on(project, tmp_path):
    """目录记录背后没有解码结果：不能拿预览/缩略图顶替原件。"""
    path, scope, rows = project
    source = OnDiskExportSource(scope, [4], archive=path)
    candidate = next(iter(source.batches()))[0]

    assert not hasattr(candidate, '_display_source_pixmap')
    assert not hasattr(candidate, 'pixmap')
    with pytest.raises(ExportSourceMissing):
        candidate.pixmap_to_bytes()
    assert candidate.original_bytes() == rows[3].payload


# ── 文件夹（资源树）导出：画布页里的未加载素材也算 ──────────────


def test_canvas_folder_assets_include_what_the_page_never_loaded(
        project, notifications):
    path, scope, rows = project
    loaded = LoadedAsset(4, 'asset-4.png', rows[3].payload, canvas=CANVAS_B)
    view = FakeView(FakeScene(path, [loaded]), scope)

    listed = list(export_workflow._canvas_assets(view, CANVAS_B, scope))

    assert [getattr(item, 'save_id', None) for item in listed] == [4, 5, 6, 7]
    assert [item.original_bytes() for item in listed[1:3]] == [
        rows[4].payload, rows[5].payload]
    assert list(export_workflow._canvas_assets(view, CANVAS_B)) == [loaded]


def test_canvas_folder_assets_without_a_directory_stay_loaded_only(
        project, notifications):
    """目录不可用时不猜范围：老样子只给已加载的那些，由界面明确拒绝。"""
    path, _scope, rows = project
    loaded = LoadedAsset(4, 'asset-4.png', rows[3].payload, canvas=CANVAS_B)
    view = FakeView(FakeScene(path, [loaded]), None)

    assert list(export_workflow._canvas_assets(view, CANVAS_B)) == [loaded]


def test_canvas_folder_export_writes_the_whole_page_from_the_directory(
        project, tmp_path, notifications, monkeypatch):
    """左侧树导出画布分区：整页素材（含没加载的）逐字节写出来。"""
    from prism.workspace_service import ResourceTreeService

    path, scope, rows = project
    tree = ResourceTreeService('p1')
    tree.createPage('canvas', '画布二', CANVAS_B)
    loaded = LoadedAsset(4, 'asset-4.png', rows[3].payload, canvas=CANVAS_B)
    scene = FakeScene(path, [loaded])
    scene.workspace_pages = [dict(id=CANVAS_B, kind='canvas',
                                  title='画布二', content=None, tags=[])]
    view = FakeView(scene, scope)
    view.parent = type('Parent', (), {'_store_current_page': lambda _self: True})()
    view.category_panel = type('Panel', (), {'resource_service': tree})()

    monkeypatch.setattr(export_workflow, 'ask_export_shape',
                        lambda parent, title: 'folder')
    from PyQt6 import QtWidgets
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory',
                        lambda *args, **kwargs: str(tmp_path/'tree'))
    def folder_worker(func, *args, **kwargs):
        worker = FakeWorker(func)
        worker.args, worker.kwargs = args, kwargs
        return worker

    monkeypatch.setattr(export_workflow.fileio, 'ThreadedIO', folder_worker)

    export_workflow.export_resource_subtree(view, 'canvas')

    assert not [text for text in notifications
                if 'needs the project directory' in text], (
        '目录可用时不该再整份拒绝')
    assert any('Exported 3 file(s)' in text for text in notifications)
    root = tmp_path/'tree'/'画布'/'画布二'
    assert sorted(p.name for p in root.iterdir()) == ['asset-4.png',
                                                      'asset-5.png',
                                                      'asset-6.png']
    assert (root/'asset-4.png').read_bytes() == rows[3].payload
    assert (root/'asset-5.png').read_bytes() == rows[4].payload
    assert (root/'asset-6.png').read_bytes() == rows[5].payload
    assert not (root/'asset-7.png').exists(), '没有原件的那张不能凭空出现'


# ── 十万级：分页 + 分批 + 可取消 ───────────────────────────────


def test_large_project_pages_in_bounded_batches(tmp_path, notifications,
                                                run_export, monkeypatch):
    """一个超过单批上限的工程：多批解析，每批有界，全部逐字节导出。"""
    rows = [Asset(index+1, CANVAS_A if index % 2 else CANVAS_B,
                  f'asset-{index+1}.png', original_bytes(index+1, size=24))
            for index in range(1100)]
    path = build_project(tmp_path/'big.prism', rows)
    scope = ProjectOpenScope(path, tmp_path/'big-catalog.sqlite')
    assert scope.build(batch_size=256).complete

    sizes, original_details = [], ProjectOpenScope.details

    def recording(self, item_ids, **kwargs):
        sizes.append(len(item_ids))
        return original_details(self, item_ids, **kwargs)

    monkeypatch.setattr(ProjectOpenScope, 'details', recording)
    view = FakeView(FakeScene(path, []), scope)

    run_export(view)

    outdir = tmp_path/'out'
    assert len(sizes) > 1, '1100 条不可能挤进一个批次'
    assert max(sizes) <= 1024, sizes
    assert len(exported_files(outdir)) == len(rows)
    for row in rows[::137]:
        assert (outdir/f'{row.id:04d}-{row.filename}').read_bytes() == row.payload


def test_cancel_during_the_directory_walk_stops_the_export(
        tmp_path, notifications, run_export, monkeypatch):
    """目录读取本身也可取消：取消后不继续把剩下的工程导出去。"""
    rows = assets(count=40, canvas=CANVAS_A)
    path = build_project(tmp_path/'cancel.prism', rows)
    scope = ProjectOpenScope(path, tmp_path/'cancel-catalog.sqlite')
    assert scope.build(batch_size=64).complete
    original_details = ProjectOpenScope.details

    def cancelling(self, item_ids, **kwargs):
        kwargs['cancel'] = lambda: True
        return original_details(self, item_ids, **kwargs)

    monkeypatch.setattr(ProjectOpenScope, 'details', cancelling)
    view = FakeView(FakeScene(path, []), scope)

    run_export(view)

    assert not exported_files(tmp_path/'out'), '取消后不该写出任何文件'