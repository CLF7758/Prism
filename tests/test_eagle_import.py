import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from PIL import Image

from prism.eagle_import import convert_library, library_root
from prism.storage.entrypoint import resolve


def library(tmp_path):
    root = tmp_path / 'outer' / 'test.library'
    (root / 'images').mkdir(parents=True)
    (root / 'metadata.json').write_text(json.dumps({'folders': [
        {'id': 'parent', 'name': '父级', 'children': [
            {'id': 'child', 'name': '子级', 'children': []},
            {'id': 'empty', 'name': '空分类', 'children': []}]}]}), encoding='utf-8')
    return root


def item(root, key, ext='png', **extra):
    directory = root / 'images' / (key + '.info')
    directory.mkdir()
    path = directory / f'{key}.{ext}'
    if ext == 'png':
        Image.new('RGB', (16, 12), 'red').save(path)
    elif ext == 'obj':
        path.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
    else:
        path.write_bytes(b'original video or attachment bytes')
    metadata = dict(id=key, name=key, ext=ext, width=16, height=12,
                    folders=[], tags=[], **extra)
    (directory / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
    return path


def read_result(entry):
    with closing(sqlite3.connect(resolve(entry) / 'metadata.sqlite')) as db:
        items = [(pk, kind, json.loads(data)) for pk, kind, data in
                 db.execute('select id,type,data from items')]
        nodes = [json.loads(row[0]) for row in db.execute('select payload from workspace_nodes')]
        pages = list(db.execute('select id,title from workspace_pages'))
    return items, nodes, pages


def test_hierarchy_metadata_multifolder_and_originals(tmp_path):
    root = library(tmp_path)
    original = item(root, 'a', annotation='图片备注', star=5)
    meta_path = original.parent / 'metadata.json'
    meta = json.loads(meta_path.read_text())
    meta.update(folders=['parent', 'child'], tags=['角色', '参考'])
    meta_path.write_text(json.dumps(meta), encoding='utf-8')
    item(root, 'deleted', isDeleted=True, modificationTime=123456)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    result = convert_library(root.parent, tmp_path / 'converted')
    items, nodes, pages = read_result(result.entry)
    assert result.folders == 3 and result.images == 2 and result.deleted == 1
    assert len(items) == 3  # One shared original placed on two canvases.
    assert {title for _, title in pages} == {'默认画布', '父级', '子级', '空分类'}
    parent = next(n for n in nodes if n['nodeType'] == 'folder' and n['title'] == '父级')
    assert all(n['parentId'] == parent['id'] for n in nodes if n['title'] in ('子级', '空分类'))
    imported = [d for _, _, d in items if d['title'] == 'a']
    assert len({d['canvasId'] for d in imported}) == 2
    assert all(d['notes'] == '图片备注' and d['rating'] == 5 and d['tags'] == ['角色', '参考'] for d in imported)
    assert next(d for _, _, d in items if d['title'] == 'deleted')['deletedAt'] == 123456
    from prism.source_store import read_archive
    for pk, _, data in items:
        source = root / 'images' / (data['eagle']['id'] + '.info') / data['filename']
        assert read_archive((str(result.entry), pk)) == source.read_bytes()
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before} == before


def test_video_and_model_use_existing_storage(tmp_path):
    root = library(tmp_path)
    item(root, 'movie', 'mp4', annotation='视频备注', star=3)
    item(root, 'model', 'obj', annotation='模型备注', star=4, isDeleted=True)
    result = convert_library(root, tmp_path / 'converted')
    rows, _, _ = read_result(result.entry)
    assert result.videos == 1 and result.models == 1
    assert rows[0][1] == 'video' and rows[0][2]['isReference'] is False
    assert rows[0][2]['notes'] == '视频备注' and rows[0][2]['rating'] == 3
    with closing(sqlite3.connect(resolve(result.entry) / 'metadata.sqlite')) as db:
        model = json.loads(db.execute('select payload from model_records').fetchone()[0])
        assert model['notes'] == '模型备注' and model['rating'] == 4 and model['deleted_at']


def test_collision_cancel_missing_original_and_source_destination(tmp_path):
    root = library(tmp_path)
    source = item(root, 'a')
    target = tmp_path / 'exists.prism'
    target.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        convert_library(root, target)
    assert target.read_bytes() == b'keep'
    from prism.storage.contracts import StorageError
    with pytest.raises(StorageError):
        convert_library(root, tmp_path / 'cancelled', cancel=lambda: True)
    assert not (tmp_path / 'cancelled.prism').exists()
    with pytest.raises(ValueError, match='之外'):
        convert_library(root, root / 'bad')
    source.unlink()
    with pytest.raises(ValueError, match='原件缺失'):
        convert_library(root, tmp_path / 'missing')
    assert not (tmp_path / 'missing.prism').exists()


def test_action_and_menu_share_existing_entry():
    from prism.actions.actions import actions
    from prism.actions import menu_structure
    assert actions['import_eagle'].callback == 'on_action_import_eagle'
    assert 'import_eagle' in repr(vars(menu_structure))


def test_saved_eagle_video_poster_and_compact_layout(tmp_path, qapp, monkeypatch):
    from PyQt6 import QtCore, QtGui, QtWidgets
    from prism import lod, image_decode
    from prism.items import PrismVideoItem
    root = library(tmp_path)
    video = item(root, 'movie', 'mp4')
    Image.new('RGB', (160,90), (30,140,80)).save(video.with_name('movie_thumbnail.png'))
    item(root, 'first')
    item(root, 'second')
    result = convert_library(root, tmp_path / 'posters')
    rows, _, _ = read_result(result.entry)
    movie = next(data for _,kind,data in rows if kind == 'video')
    assert movie['displayThumbnail'][:2] == [160,90]
    assert len(__import__('base64').b64decode(movie['displayOverview'][2])) == movie['displayOverview'][0]*movie['displayOverview'][1]*4
    with closing(sqlite3.connect(resolve(result.entry) / 'metadata.sqlite')) as db:
        rects = [(x,y,16*scale,12*scale) for x,y,scale in db.execute('select x,y,scale from items')]
    for n, (x,y,w,h) in enumerate(rects):
        for xx,yy,ww,hh in rects[n+1:]:
            assert x+w <= xx or xx+ww <= x or y+h <= yy or yy+hh <= y
    assert max(x+w for x,y,w,h in rects) < 360
    item_video = PrismVideoItem(filename='movie.mp4')
    item_video._prism_source = (str(result.entry), next(pk for pk,kind,_ in rows if kind == 'video'))
    PrismVideoItem.create_from_data(item=item_video, data=movie)
    assert item_video._thumbnail_ready
    assert not hasattr(item_video, '_source_blob')  # This was the actual crash path.
    image_decode.request_display(item_video, 600)
    def wrong_decoder(*args, **kwargs):
        raise AssertionError('A video must never enter the image decode queue')
    monkeypatch.setattr(image_decode, 'request_display', wrong_decoder)
    assert lod.display_pixmap(item_video, 600).toImage().pixelColor(0,0) == QtGui.QColor(30,140,80)
    surface = QtGui.QImage(300,200,QtGui.QImage.Format.Format_ARGB32)
    painter = QtGui.QPainter(surface)
    try:
        assert lod.draw_image(painter, item_video, QtCore.QRectF(0,0,300,200))
    finally:
        painter.end()
    saved = item_video.get_extra_save_data()
    assert saved['displayThumbnail'] == movie['displayThumbnail']
    scene = QtWidgets.QGraphicsScene()
    scene.addItem(item_video)
    assert item_video._control_bar is None  # No widget construction on page load.
    item_video._ensure_control_bar()
    assert item_video._control_bar is not None
    item_video.cleanup()
    scene.clear()


@pytest.mark.parametrize('with_existing_assets', [False, True])
def test_open_project_import_keeps_project_and_saves_all_domains(main_window, qtbot, monkeypatch, tmp_path, with_existing_assets):
    from prism.actions.eagle_workflow import _apply_current
    from prism.eagle_import import prepare_current_import
    from prism.fileio.sql import SQLiteIO
    from prism.storage.entrypoint import convert
    from prism.model_library.multimaterial import bundled_runtime
    from prism.model_library.service import ModelLibraryService

    root = library(tmp_path)
    picture = item(root, 'picture', annotation='保留备注', star=4)
    data_path = picture.parent / 'metadata.json'
    data = json.loads(data_path.read_text())
    data.update(folders=['child','empty'], tags=['Eagle标签'])
    data_path.write_text(json.dumps(data), encoding='utf-8')
    video = item(root, 'movie', 'mp4')
    Image.new('RGB',(160,90),'green').save(video.with_name('movie_thumbnail.png'))
    item(root, 'mesh', 'obj', annotation='模型备注', star=3)
    view = main_window.view
    if with_existing_assets:
        from prism.fileio import _load_image_item
        previous = tmp_path / 'already-here.png'
        Image.new('RGB',(16,12),'blue').save(previous)
        existing = _load_image_item(str(previous), as_record=True)
        existing.title = '已有素材'
        view.scene.addItem(existing)
    seed = tmp_path / 'seed.prism'
    io = SQLiteIO(str(seed), view.scene, create_new=True)
    try:
        io.write()
    finally:
        io._close_connection()
    entry = convert(seed, tmp_path / 'current.prism')
    # Model bytes and scene acknowledgements remain real; GPU preview is outside this test.
    monkeypatch.setattr(bundled_runtime, 'available', lambda: False)
    monkeypatch.setattr(ModelLibraryService, 'probe_native', lambda *args: None)
    monkeypatch.setattr(ModelLibraryService, 'render_cover', lambda *args: None)
    with qtbot.waitSignal(view.project_open_finished, timeout=15000):
        view.open_from_file(str(entry))
    qtbot.waitUntil(lambda: not view._file_operation_pending(), timeout=15000)
    current_filename = view.filename
    project_id = view.scene.project_id
    original_pages = {p['id'] for p in view.scene.workspace_pages}
    if with_existing_assets:
        existing = next(i for i in view.scene.items_for_save() if i.title == '已有素材')
        existing.notes = '导入前尚未保存的备注'
    monkeypatch.setattr(main_window, 'open_workspace_page', lambda *args: None)
    monkeypatch.setattr(main_window, '_store_current_page', lambda *args: True)
    main_window.model_composition.ensure()
    qtbot.waitUntil(lambda: main_window.model_composition.service is not None, timeout=10000)
    outcome = _apply_current(view, prepare_current_import(root))
    qtbot.waitUntil(lambda: not view._import_in_progress and not view.worker.isRunning(), timeout=15000)
    service = main_window.model_composition.service
    qtbot.waitUntil(lambda: not service.pending_writes and len(view.scene.model_records)==1, timeout=15000)
    assert view.filename == current_filename and view.scene.project_id == project_id
    assert original_pages <= {p['id'] for p in view.scene.workspace_pages}
    imported = list(view.scene.items_for_save())
    pictures = [i for i in imported if i.title == 'picture']
    assert len(pictures) == 2 and len({i.canvas_id for i in pictures}) == 2
    assert all(i.notes == '保留备注' and i._rating == 4 and i._tags == ['Eagle标签'] for i in pictures)
    movie = next(i for i in imported if getattr(i,'TYPE',None)=='video')
    assert movie._thumbnail_ready and movie._is_reference is False
    assert movie._control_bar is None
    assert view.scene.model_records[0]['notes'] == '模型备注'
    view.do_save(current_filename, create_new=False)
    qtbot.waitUntil(lambda: not getattr(view,'_save_preparing',False)
                   and getattr(view,'_save_operation',None) is None, timeout=20000)
    assert view.last_save_outcome.confirmed, view.last_save_outcome.error
    with closing(sqlite3.connect(resolve(entry)/'metadata.sqlite')) as db:
        assert db.execute('select count(*) from items').fetchone()[0] == 3+int(with_existing_assets)
        if with_existing_assets:
            saved = [json.loads(row[0]) for row in db.execute('select data from items')]
            assert next(d for d in saved if d['title']=='已有素材')['notes'] == '导入前尚未保存的备注'
        assert db.execute('select count(*) from model_records').fetchone()[0] == 1
        assert db.execute('select count(*) from workspace_pages').fetchone()[0] >= len(original_pages)+4


def test_open_project_menu_never_asks_for_a_new_destination(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from prism.actions import eagle_workflow, storage_workflow
    from PyQt6 import QtWidgets
    root = library(tmp_path)
    chosen = []
    view = SimpleNamespace(filename='already-open.prism', scene=SimpleNamespace(project_id='current'),
        _allow_import_during_open=lambda: True,
        _choose_project_destination=lambda *a,**k: pytest.fail('Must not ask for a new project location'))
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory', lambda *a,**k: str(root))
    monkeypatch.setattr(storage_workflow, 'run', lambda view,title,job,done,**kwargs: chosen.append(title))
    eagle_workflow.import_eagle(view)
    assert chosen == ['导入 Eagle 资料库']
