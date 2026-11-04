"""Real project originals -> persistent native file URLs, without a renderer."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui

from prism.model_library.transfer import prepare_model_transfer
from prism.widgets.library_transfer import start_library_drag, read_library_drag
from tests.model_library.test_export import (project, context, repository, importer,
    exporter, import_model, SAMPLES)


def service(repository, exporter):
    return SimpleNamespace(_closed=False, repository=repository, exporter=exporter)


def test_original_model_and_all_textures_are_native_files(importer, repository, exporter, tmp_path):
    folder = SAMPLES/'assimp_spider'
    key = import_model(importer, folder/'spider.obj')
    payload = repository.get_payload(key)
    result = prepare_model_transfer(service(repository, exporter), [key], tmp_path/'handoff')
    files = [Path(path) for path in result['paths']]
    assert {path.name for path in files} == {'spider.obj', 'SpiderTex.jpg'}
    assert result['textures'] == 1
    assert len(result['missing']) == 4  # source sample has four missing textures
    assert (files[0].parent/'spider.mtl').read_bytes() == (folder/'spider.mtl').read_bytes()
    for path in files:
        assert path.read_bytes() == (folder/path.name).read_bytes()
    assert repository.get_payload(key) == payload
    assert not list((tmp_path/'handoff').rglob('*.prism.json'))
    # A second gesture reuses verified private files, not a second blob read.
    exporter._copy_verified = Mock(side_effect=AssertionError('repeated blob copy'))
    again = prepare_model_transfer(service(repository, exporter), [key], tmp_path/'handoff')
    assert again['paths'] == result['paths']


def test_project_added_texture_is_included_and_bad_cache_is_repaired(importer, repository, exporter, tmp_path):
    key = import_model(importer, SAMPLES/'assimp_spider'/'spider.obj')
    texture = tmp_path/'new.png'
    from PIL import Image
    Image.new('RGB', (4,4), 'red').save(texture)
    saved = repository.resources.put_file(texture)
    payload = repository.get_payload(key)
    payload['multimaterial'] = dict(resources={'new': dict(attachment_id=saved.attachment_id,
        locator=texture.name, sha256=saved.sha256, size=saved.size_bytes)})
    real_get = repository.get_payload
    repository.get_payload = lambda value: payload if value == key else real_get(value)
    result = prepare_model_transfer(service(repository, exporter), [key, key], tmp_path/'handoff')
    assert result['textures'] == 2
    copied = next(Path(path) for path in result['paths'] if Path(path).name == texture.name)
    assert copied.read_bytes() == texture.read_bytes()
    copied.write_bytes(b'broken private cache')
    prepare_model_transfer(service(repository, exporter), [key], tmp_path/'handoff')
    assert copied.read_bytes() == texture.read_bytes()


def test_batch_same_names_do_not_overwrite_each_other(importer, repository, exporter, tmp_path):
    from PIL import Image
    ids, originals = [], {}
    for index in range(2):
        folder = tmp_path/str(index)
        folder.mkdir()
        (folder/'model.obj').write_text(f'mtllib model.mtl\n# {index}\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl A\nf 1 2 3\n')
        (folder/'model.mtl').write_text('newmtl A\nmap_Kd Color.png\n')
        Image.new('RGB', (4,4), ('red','blue')[index]).save(folder/'Color.png')
        ids.append(import_model(importer, folder/'model.obj'))
        for name in ('model.obj','Color.png'):
            blob = (folder/name).read_bytes()
            originals[hashlib.sha256(blob).hexdigest()] = blob
    result = prepare_model_transfer(service(repository, exporter), ids, tmp_path/'handoff')
    files = [Path(path) for path in result['paths']]
    assert len(files) == 4
    assert len({path.stem.casefold() for path in files}) == 4
    for path in files:
        blob = path.read_bytes()
        assert originals[hashlib.sha256(blob).hexdigest()] == blob
    assert all((path.parent/'model.mtl').is_file() for path in files if path.suffix == '.obj')


def test_combined_drag_retains_internal_move_and_file_urls(qapp, monkeypatch, tmp_path):
    file = tmp_path/'model.fbx'
    file.write_bytes(b'original')
    folders = SimpleNamespace(project_id='project')
    def execute(drag, supported, default):
        assert supported & QtCore.Qt.DropAction.CopyAction
        assert supported & QtCore.Qt.DropAction.MoveAction
        assert default == QtCore.Qt.DropAction.CopyAction
        assert read_library_drag(drag.mimeData(), folders)['ids'] == ['one','two']
        assert [Path(url.toLocalFile()) for url in drag.mimeData().urls()] == [file]
        return QtCore.Qt.DropAction.CopyAction
    monkeypatch.setattr(QtGui.QDrag, 'exec', execute)
    assert start_library_drag(None, folders, 'model', ['one','two'], [file]) == QtCore.Qt.DropAction.CopyAction


def test_missing_main_and_closed_project_never_start_transfer(importer, repository, exporter, tmp_path):
    key = import_model(importer, SAMPLES/'assimp_spider'/'spider.obj')
    owner = service(repository, exporter)
    owner._closed = True
    with pytest.raises(ValueError, match='关闭'):
        prepare_model_transfer(owner, [key], tmp_path/'out')
    owner._closed = False
    payload = repository.get_payload(key)
    next(file for file in payload['files'] if file['role']=='main')['missing'] = True
    repository.get_payload = lambda _: payload
    with pytest.raises(ValueError, match='原件缺失'):
        prepare_model_transfer(owner, [key], tmp_path/'out')


@pytest.mark.parametrize('scope,gesture,pressed', [('old',10,True), ('current',9,True), ('current',10,False)])
def test_released_or_old_preparation_never_starts_drag(scope, gesture, pressed, monkeypatch):
    from PyQt6 import QtWidgets
    from prism.widgets.model_library import ModelLibrary
    owner = object()
    worker = SimpleNamespace(service=owner, model_id=('one',))
    page = SimpleNamespace(_workers={id(worker): worker}, _transfer_worker=worker,
        _scope_token='current', service=owner, cards=SimpleNamespace(mouse_sequence=10),
        selection=SimpleNamespace(freeze=lambda: SimpleNamespace(model_ids=('one',))),
        status=Mock())
    drag = Mock(side_effect=AssertionError('stale native drag'))
    monkeypatch.setattr('prism.widgets.library_transfer.start_library_drag', drag)
    monkeypatch.setattr(QtWidgets.QApplication, 'mouseButtons', lambda:
        QtCore.Qt.MouseButton.LeftButton if pressed else QtCore.Qt.MouseButton.NoButton)
    ModelLibrary._transfer_ready(page, (worker, scope, gesture,
        dict(paths=['one.fbx'], textures=0, missing=[]), None))
    assert not drag.called
    assert page._transfer_worker is None
