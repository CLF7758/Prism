import hashlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PyQt6 import QtCore, QtWidgets, QtTest
from prism.external_drag import file_mime
from prism.material_textures import prepare_texture
from prism.material_transfer import prepare_texture_transfer
from tests.test_material_cards import javascript


def create_sources(window, tmp_path):
    service = window.material_library.records
    records = []
    for index in range(2):
        material = service.create(name=f'Material {index}')
        directory = tmp_path/str(index)
        directory.mkdir()
        file = directory/'Color.png'
        Image.new('RGBA',(16,16),(index*80,30,160,80)).save(file)
        service.configure(material['id'],slot='basecolor',prepared=prepare_texture(file))
        records.append(service.get(material['id']))
    return service, records


def test_single_multi_originals_and_collision_names(main_window,tmp_path):
    service, records = create_sources(main_window,tmp_path)
    root = tmp_path/'transfer'
    result = prepare_texture_transfer(records,service.scene.attachments,root)
    assert len(result['paths']) == 2 and result['renamed'] == 2
    paths = [Path(path) for path in result['paths']]
    assert len({path.stem.casefold() for path in paths}) == 2
    assert {hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == {
        file['sha256'] for record in records for file in record['files']}
    before = [path.stat().st_mtime_ns for path in paths]
    again = prepare_texture_transfer(records,service.scene.attachments,root)
    assert again['paths'] == result['paths']
    assert [path.stat().st_mtime_ns for path in paths] == before
    single = prepare_texture_transfer(records[:1],service.scene.attachments,root)
    assert Path(single['paths'][0]).name == 'Color.png'
    mime = file_mime(result['paths'])
    assert mime.hasUrls() and mime.hasFormat('text/uri-list')
    assert {Path(url.toLocalFile()) for url in mime.urls()} == {Path(path) for path in result['paths']}
    assert not mime.hasImage()  # Native file payload, not a rendered thumbnail.


def test_shared_packed_texture_is_sent_once(main_window,tmp_path):
    service, records = create_sources(main_window,tmp_path)
    image = tmp_path/'packed.png'
    Image.new('RGB',(8,8),(40,80,160)).save(image)
    prepared = prepare_texture(image)
    for slot,channel in [('ao','R'),('roughness','G'),('metallic','B')]:
        service.configure(records[0]['id'],slot=slot,prepared=prepared,channel=channel)
    record = service.get(records[0]['id'])
    result = prepare_texture_transfer([record,record],service.scene.attachments,tmp_path/'out')
    assert len(result['paths']) == 2  # color + the original packed map, not 3 split maps.
    assert Path(result['paths'][1]).read_bytes() == prepared[1]


def test_replaced_unbound_history_stays_stored_but_is_not_dragged(main_window,tmp_path):
    service, records = create_sources(main_window,tmp_path)
    image = tmp_path/'new-color.png'
    Image.new('RGB',(8,8),'yellow').save(image)
    prepared = prepare_texture(image)
    service.configure(records[0]['id'],slot='basecolor',prepared=prepared)
    record = service.get(records[0]['id'])
    assert len(record['files']) == 2
    result = prepare_texture_transfer([record],service.scene.attachments,tmp_path/'out')
    assert len(result['paths']) == 1
    assert Path(result['paths'][0]).read_bytes() == prepared[1]


def test_dom_drag_uses_whole_selection_and_stale_events_rejected(main_window,qtbot,tmp_path):
    main_window.show();main_window.open_material_library()
    service, records = create_sources(main_window,tmp_path)
    page = main_window.material_library
    qtbot.waitUntil(lambda:page.materials.ready,timeout=15000)
    qtbot.waitUntil(lambda:javascript(page.materials,"document.querySelectorAll('.asset-card').length",qtbot)==2)
    page.materials.select_record(records[0]['id'])
    page.materials.select_record(records[1]['id'],True)
    root = tmp_path/'transfer'
    with patch('prism.material_transfer.transfer_directory',return_value=root), \
         patch.object(QtWidgets.QApplication,'mouseButtons',return_value=QtCore.Qt.MouseButton.LeftButton), \
         patch('prism.external_drag.start_file_drag',return_value=QtCore.Qt.DropAction.CopyAction) as drag:
        javascript(page.materials,"document.querySelector('.asset-card').dispatchEvent(new DragEvent('dragstart',{bubbles:true,cancelable:true}))",qtbot)
        qtbot.waitUntil(lambda:drag.called,timeout=15000)
        paths = drag.call_args.args[1]
        assert len(paths) == 2 and all(Path(path).is_file() for path in paths)
        assert page.materials.selected_ids == {r['id'] for r in records}
        qtbot.waitUntil(lambda:not page._drag_busy,timeout=15000)
        count = drag.call_count
        page.materials.bridge.dragTextures(page.materials._dom_id(records[0]['id']),page.materials.generation-1)
        qtbot.wait(50)
        assert drag.call_count == count


def test_released_mouse_prepares_but_does_not_start_ghost_drag(main_window,qtbot,tmp_path):
    main_window.open_material_library()
    service, records = create_sources(main_window,tmp_path)
    page = main_window.material_library
    with patch('prism.material_transfer.transfer_directory',return_value=tmp_path/'out'), \
         patch.object(QtWidgets.QApplication,'mouseButtons',return_value=QtCore.Qt.MouseButton.NoButton), \
         patch('prism.external_drag.start_file_drag') as drag:
        page.drag_textures([records[0]['id']])
        qtbot.waitUntil(lambda:not page._drag_busy,timeout=15000)
        assert not drag.called
        assert list((tmp_path/'out').rglob('*.png'))


def test_left_mouse_gesture_in_real_card_webview(main_window,qtbot,tmp_path):
    main_window.resize(1450,950);main_window.show();main_window.open_material_library()
    service, records = create_sources(main_window,tmp_path)
    page = main_window.material_library
    qtbot.waitUntil(lambda:page.materials.ready,timeout=15000)
    qtbot.waitUntil(lambda:javascript(page.materials,"document.querySelectorAll('.asset-card').length",qtbot)==2)
    position = javascript(page.materials,"(()=>{const r=document.querySelector('.card-image').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+50}})()",qtbot)
    target = page.materials.web.focusProxy()
    assert target is not None
    point = QtCore.QPoint(round(position['x']),round(position['y']))
    with patch('prism.material_transfer.transfer_directory',return_value=tmp_path/'out'), \
         patch.object(QtWidgets.QApplication,'mouseButtons',return_value=QtCore.Qt.MouseButton.LeftButton), \
         patch('prism.external_drag.start_file_drag',return_value=QtCore.Qt.DropAction.CopyAction) as drag:
        QtTest.QTest.mousePress(target,QtCore.Qt.MouseButton.LeftButton,pos=point)
        for delta in (12,32,60):
            QtTest.QTest.mouseMove(target,point+QtCore.QPoint(delta,0),100)
            qtbot.wait(100)
        qtbot.waitUntil(lambda:drag.called,timeout=15000)
        QtTest.QTest.mouseRelease(target,QtCore.Qt.MouseButton.LeftButton,pos=point+QtCore.QPoint(60,0))
        assert len(drag.call_args.args[1]) == 1


def test_old_preparation_cannot_start_a_new_mouse_gesture(main_window,qtbot,tmp_path):
    import threading
    main_window.open_material_library()
    service, records = create_sources(main_window,tmp_path)
    page = main_window.material_library
    started, release = threading.Event(), threading.Event()
    def prepare(*args):
        started.set()
        release.wait(5)
        return dict(paths=[str(tmp_path/'0/Color.png')],materials=1,empty=0,renamed=0)
    with patch('prism.material_transfer.prepare_texture_transfer',side_effect=prepare), \
         patch.object(QtWidgets.QApplication,'mouseButtons',return_value=QtCore.Qt.MouseButton.LeftButton), \
         patch('prism.external_drag.start_file_drag') as drag:
        page.drag_textures([records[0]['id']])
        qtbot.waitUntil(started.is_set,timeout=10000)
        page.materials.eventFilter(page,QtCore.QEvent(QtCore.QEvent.Type.MouseButtonRelease))
        page.materials.eventFilter(page,QtCore.QEvent(QtCore.QEvent.Type.MouseButtonPress))
        release.set()
        qtbot.waitUntil(lambda:not page._drag_busy,timeout=10000)
        assert not drag.called
