from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import os
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.widgets.asset_browser import AssetModel
from prism.widgets.asset_canvas import AssetCanvas


def test_canvas_groups_cache_pixels_and_release_to_detail(qapp, monkeypatch):
    parent = QtWidgets.QWidget()
    parent.mode = 'all'
    host = AssetCanvas(parent)
    host.viewport().setFixedSize(800,800)
    monkeypatch.setattr(host, 'isVisible', lambda: True)
    model = AssetModel()
    host.setModel(model)
    calls = []
    class Delegate:
        def paint(self, painter, option, index):
            calls.append(index.row())
            painter.fillRect(option.rect, QtGui.QColor('red' if index.row()%2 else 'blue'))
    host.setItemDelegate(Delegate())
    model.replace([SimpleNamespace(crop=QtCore.QRectF(0,0,120,100), _image_size=QtCore.QSize(120,100),
                                  _canvas_id=f'canvas-{i//100}') for i in range(2000)])
    groups = host._composites
    assert len(groups.groups) == 20
    viewport = host._content_bounds
    image = QtGui.QImage(800,800,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    def paint(scale):
        host.resize(800,800)
        host.setTransform(QtGui.QTransform.fromScale(scale,scale))
        host.centerOn(viewport.center())
        painter=QtGui.QPainter(image)
        painter.scale(scale,scale)
        try:
            return groups.paint(painter,viewport)
        finally:
            painter.end()
    assert len(paint(.05)) == 2000
    host.setTransform(QtGui.QTransform.fromScale(.001, .001))
    with monkeypatch.context() as patch:
        patch.setattr(host, 'visible_cards', lambda *_: pytest.fail('Overview must not scan/sort the library'))
        assert host.queue_visible_details()
    host.setTransform(QtGui.QTransform.fromScale(.05, .05))
    groups.camera_changed = 0  # Explicitly benchmark the settled-camera phase.
    for _ in range(1000):
        if not groups.pending:
            break
        groups._work()
    assert not groups.pending
    assert len(calls) == 2000
    assert paint(.05) == []
    assert len(calls) == 2000, 'Warm overview must draw composites rather than 2000 images'
    assert groups.draws == 20
    # A recording tile's transform must not release distant groups to
    # thousands of individual delegate calls.
    recorder = QtGui.QPainter(image)
    try:
        recorder.scale(1, 1)
        assert groups.paint(recorder, viewport) == []
    finally:
        recorder.end()
    assert len(calls) == 2000
    for row, expected in ((0, 'blue'), (1, 'red')):
        point = host.cards[row].sceneBoundingRect().center()*.05
        color = image.pixelColor(int(point.x()), int(point.y()))
        assert (color.blue() > color.red()) if expected == 'blue' else (color.red() > color.blue())
    assert paint(1) is None, 'Close views must use original per-image detail'
    assert not groups.pending
    assert not groups.timer.isActive()
    before=len(groups.cache)
    groups.invalidate(100)
    assert len(groups.cache) == before-1
    assert groups.bytes <= groups.BUDGET
    assert len(paint(.1)) == 100, 'Reuse the other groups while a new camera level is prepared'
    assert set(groups.cache) == {key for keys in groups.by_group.values() for key in keys}
    host.cards[0].setPos(9999,9999)
    assert not groups.groups, 'Manual placement must not leave a stale group bounding box'
    parent.close()


def test_group_refresh_waits_until_project_install_finishes(view, qtbot, monkeypatch):
    manager=view.scene.groups
    refresh=Mock(wraps=manager.refresh)
    monkeypatch.setattr(manager,'refresh',refresh)
    view._project_opening=True
    manager.schedule_structure()
    qtbot.wait(80)
    assert refresh.call_count == 0
    assert manager._structure_dirty
    view._project_opening=False
    qtbot.waitUntil(lambda:refresh.call_count==1,timeout=1000)


def test_large_document_library_only_creates_editor_when_opened(main_window, qtbot):
    window=main_window
    window.view.scene.workspace_pages=[dict(id=f'large-doc-{i}',kind='document',title=f'Doc {i}',
                                           content=f'<p>Document {i}</p>',tags=[],parent=None)
                                       for i in range(500)]
    window._sync_page_panels()
    assert len(window._page_panels)==0
    window.open_workspace_page('document','large-doc-499')
    assert set(window._page_panels)=={'large-doc-499'}
    panel=window._page_panels['large-doc-499']
    qtbot.waitUntil(lambda:panel._ready and not panel._pending_push,timeout=10000)
    window._sync_page_panels()
    assert window._page_panels['large-doc-499'] is panel


@pytest.mark.parametrize('count,expected_groups', [(50,0),(100,1),(1000,8),(10000,79)])
def test_image_count_tiers_keep_group_work_bounded(qapp, qtbot, count, expected_groups):
    parent=QtWidgets.QWidget();parent.mode='all'
    host=AssetCanvas(parent);model=AssetModel();host.setModel(model)
    model.replace([SimpleNamespace(crop=QtCore.QRectF(0,0,120,100),_image_size=QtCore.QSize(120,100),
                                  _canvas_id='one-canvas') for _ in range(count)])
    qtbot.waitUntil(lambda: host._build_state is None)
    assert len(host._composites.groups)==expected_groups
    assert all(len(cards)<=128 for _bounds,cards in host._composites.groups)
    parent.close()


def test_default_canvas_navigation_uses_packed_browser_without_moving_sources(main_window):
    from tests.test_asset_browser import photo
    item=photo(main_window,'default-canvas','Source')
    item.setPos(777,333)
    main_window._open_resource_page('canvas','default-canvas')
    assert main_window.tabs.currentWidget() is main_window.asset_browser
    assert main_window.asset_browser.scope_pages=={'default-canvas'}
    assert item.pos()==QtCore.QPointF(777,333)


def test_zoom_status_reuses_counts_instead_of_scanning_library(main_window, monkeypatch):
    window=main_window
    window._update_status({'refresh':True})
    scan=Mock(wraps=window.view.category_panel.catalog.active_items)
    monkeypatch.setattr(window.view.category_panel.catalog,'active_items',scan)
    for _ in range(20):
        window._update_status({})
    assert scan.call_count==0


def test_far_cover_does_not_start_a_partial_whole_library_redraw(main_window, monkeypatch):
    canvas=main_window.asset_browser.list
    canvas.setTransform(QtGui.QTransform.fromScale(.01,.01))
    canvas._composites.pending[(0,32)]=(None,0)
    region_ready=Mock(return_value=True)
    monkeypatch.setattr(canvas._composites,'ready_for',region_ready)
    assert not canvas.can_build_display_tile((0,0,0))
    region_ready.assert_not_called()
    canvas.setTransform(QtGui.QTransform.fromScale(.1,.1))
    assert canvas.can_build_display_tile((0,0,0))


def test_fast_wheel_preserves_accumulated_steps_and_clamps_zoom(main_window):
    canvas=main_window.asset_browser.list
    canvas.setTransform(QtGui.QTransform())
    def wheel(delta):
        event=QtGui.QWheelEvent(QtCore.QPointF(100,100),QtCore.QPointF(100,100),
                               QtCore.QPoint(),QtCore.QPoint(0,delta),
                               QtCore.Qt.MouseButton.NoButton,QtCore.Qt.KeyboardModifier.NoModifier,
                               QtCore.Qt.ScrollPhase.NoScrollPhase,False)
        canvas.wheelEvent(event)
    wheel(480)
    assert canvas.transform().m11()==pytest.approx(1.2**4)
    wheel(-480)
    assert canvas.transform().m11()==pytest.approx(1)
    wheel(1440)
    assert canvas.transform().m11()==pytest.approx(8)


def test_current_wheel_region_gets_screen_detail_before_edges(main_window, monkeypatch):
    from prism import lod
    canvas=main_window.asset_browser.list
    model=AssetModel()
    canvas.setModel(model)
    model.replace([SimpleNamespace(TYPE='pixmap',crop=QtCore.QRectF(0,0,2000,1000),
                                   _image_size=QtCore.QSize(2000,1000),_canvas_id='test') for _ in range(20)])
    canvas.setTransform(QtGui.QTransform.fromScale(2,2))
    canvas.centerOn(canvas.cards[10].sceneBoundingRect().center())
    canvas._detail_focus=QtCore.QPoint(10,10)
    rect=canvas.mapToScene(canvas.viewport().rect()).boundingRect()
    visible=canvas.visible_cards(rect)
    focus=canvas.mapToScene(canvas._detail_focus)
    expected=min(visible,key=lambda card:(card.sceneBoundingRect().center()-focus).manhattanLength())
    calls=[]
    monkeypatch.setattr(lod,'display_pixmap',lambda item,pixels,owner:calls.append((item,pixels)))
    assert not canvas.queue_visible_details()
    assert calls[0][0] is model.items[expected.row]
    assert calls[0][1]>192, 'Large on-screen images must request more than the base preview'


def test_zoomed_group_replaces_blurred_cover_with_actual_sharp_pixels(main_window, qtbot, tmp_path):
    count=2200
    import base64
    from prism.asset_records import ImageRecord
    pattern=QtGui.QImage(256,128,QtGui.QImage.Format.Format_RGB32)
    painter=QtGui.QPainter(pattern)
    for y in range(0,128,16):
        for x in range(0,256,16):
            painter.fillRect(x,y,16,16,QtGui.QColor('red' if (x//16+y//16)%2 else 'blue'))
    painter.end()
    path=tmp_path/'detail-pattern.png'
    assert pattern.save(str(path))
    overview=[8,8,base64.b64encode(bytes([127,0,127,255])*64).decode('ascii')]
    for index in range(count):
        item=ImageRecord({'data':{'imageWidth':256,'imageHeight':128,
                         'canvasId':f'block-{index//100}','displayOverview':overview}},None)
        item._source_path=str(path)
        main_window.view.scene.addItem(item)
    main_window.resize(1400,900);main_window.show()
    browser=main_window.asset_browser
    browser.open('all')
    canvas=browser.list
    qtbot.waitUntil(lambda: len(canvas.cards) == count and canvas._build_state is None)
    target=canvas.cards[count//2]
    canvas.setTransform(QtGui.QTransform.fromScale(.02,.02))
    canvas.centerOn(target.sceneBoundingRect().center())
    canvas.viewport().grab();qtbot.wait(200)
    canvas.setTransform(QtGui.QTransform.fromScale(.15,.15))
    canvas.centerOn(target.sceneBoundingRect().center())
    def sharp_pixels():
        image=canvas.viewport().grab().toImage()
        rect=canvas.viewportTransform().mapRect(target.sceneBoundingRect()).toAlignedRect().adjusted(2,2,-2,-2)
        assert image.rect().contains(rect)
        differences=[image.pixelColor(x,y).red()-image.pixelColor(x,y).blue()
                     for y in range(rect.top(),rect.bottom()) for x in range(rect.left(),rect.right())]
        return max(differences)-min(differences)>180
    qtbot.waitUntil(sharp_pixels,timeout=5000)


@pytest.mark.parametrize('count', [int(os.environ['PRISM_PIXEL_COUNT'])] if 'PRISM_PIXEL_COUNT' in os.environ else [100, 1000, 2200, 10000])
def test_all_visible_groups_publish_source_pixels(main_window, qtbot, tmp_path, count):
    import base64
    from prism.asset_records import ImageRecord
    pattern=QtGui.QImage(256,128,QtGui.QImage.Format.Format_RGB32)
    painter=QtGui.QPainter(pattern)
    for y in range(0,128,32):
        for x in range(0,256,64):
            painter.fillRect(x,y,64,32,QtGui.QColor('red' if (x//64+y//32)%2 else 'blue'))
    painter.end()
    path=tmp_path/'detail-pattern.png'
    assert pattern.save(str(path))
    overview=[8,8,base64.b64encode(bytes([127,0,127,255])*64).decode('ascii')]
    for index in range(count):
        item=ImageRecord({'data':{'imageWidth':256,'imageHeight':128,
                         'canvasId':f'block-{index//100}','displayOverview':overview}},None)
        item._source_path=str(path)
        main_window.view.scene.addItem(item)
    main_window.resize(1400,900);main_window.show()
    browser=main_window.asset_browser
    browser.open('all')
    canvas=browser.list
    qtbot.waitUntil(lambda: len(canvas.cards) == count and canvas._build_state is None)
    target=canvas.cards[count//2]
    canvas.setTransform(QtGui.QTransform.fromScale(.02,.02))
    canvas.centerOn(target.sceneBoundingRect().center())
    canvas.viewport().grab();qtbot.wait(200)
    canvas.setTransform(QtGui.QTransform.fromScale(.15,.15))
    canvas.centerOn(target.sceneBoundingRect().center())
    def sharp_pixels():
        image=canvas.viewport().grab().toImage()
        rect=canvas.viewportTransform().mapRect(target.sceneBoundingRect()).toAlignedRect().adjusted(2,2,-2,-2)
        assert image.rect().contains(rect)
        differences=[image.pixelColor(x,y).red()-image.pixelColor(x,y).blue()
                     for y in range(rect.top(),rect.bottom()) for x in range(rect.left(),rect.right())]
        return max(differences)-min(differences)>180
    qtbot.waitUntil(sharp_pixels,timeout=5000)
    # Cache entries alone are not proof that composite/tile pixels updated.
    # Check every drawable image patch in the real viewport without calling
    # display_pixmap or requesting decoder work from the test.
    def all_visible_sharp():
        image=canvas.viewport().grab().toImage()
        viewport=canvas.mapToScene(canvas.viewport().rect()).boundingRect()
        checked=0
        for card in canvas.visible_cards(viewport):
            rect=canvas.viewportTransform().mapRect(card.sceneBoundingRect()).toAlignedRect().adjusted(2,2,-2,-2)
            rect=rect.intersected(image.rect())
            if rect.width()<6 or rect.height()<6:
                continue
            checked+=1
            values=[image.pixelColor(x,y).red()-image.pixelColor(x,y).blue()
                    for y in range(rect.top(),rect.bottom()) for x in range(rect.left(),rect.right())]
            if max(values)-min(values)<=180:
                all_visible_sharp.missing=dict(row=card.row,rect=[rect.x(),rect.y(),rect.width(),rect.height()],
                                              contrast=max(values)-min(values),
                                              preview_size=getattr(browser.model.items[card.row], '_preview_pixmap', None).size().width()
                                              if getattr(browser.model.items[card.row], '_preview_pixmap', None) else None,
                                              pending_groups=len(canvas._composites.pending),stale_groups=len(canvas._composites.stale))
                return False
        assert checked, 'Pixel checks must cover visible images'
        return True
    from pathlib import Path
    import json
    output=Path('artifacts/scale-audit')
    output.mkdir(parents=True,exist_ok=True)
    passed=False
    try:
        qtbot.waitUntil(all_visible_sharp,timeout=15000)
        passed=True
    finally:
        assert canvas.viewport().grab().save(str(output/f'pixel-coverage-{count}-mid.png'))
        (output/f'pixel-coverage-{count}-diagnostic.json').write_text(json.dumps(dict(passed=passed,last_transient_missing=getattr(all_visible_sharp,'missing',{}))),encoding='utf-8')


def test_crop_metadata_change_disables_saved_overview_shortcut(qapp, monkeypatch):
    parent = QtWidgets.QWidget()
    parent.mode = 'all'
    host = AssetCanvas(parent)
    model = AssetModel()
    host.setModel(model)
    model.replace([SimpleNamespace(crop=QtCore.QRectF(0, 0, 120, 100),
                                  _image_size=QtCore.QSize(120, 100), _canvas_id='a')])
    host.setTransform(QtGui.QTransform.fromScale(.001, .001))
    visible = Mock(return_value=[])
    monkeypatch.setattr(host, 'visible_rows', visible)
    assert host.queue_visible_details()
    visible.assert_not_called()
    model.items[0].crop = QtCore.QRectF(0, 0, .01, .01)
    model.dataChanged.emit(model.index(0), model.index(0))
    assert host.queue_visible_details()
    visible.assert_called_once()
    parent.close()


def test_decoded_detail_keeps_partial_group_but_source_edits_cancel_it(qapp):
    from prism.canvas_composites import CanvasComposites
    host = QtWidgets.QWidget()
    groups = CanvasComposites(host)
    groups.groups = [(QtCore.QRectF(0, 0, 100, 100), [])]
    groups.row_group = {0: 0}
    key = (0, 32)
    partial = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_RGB32)
    groups.pending[key] = (partial, 40)
    for _ in range(20):
        groups.invalidate(0, preserve=True)
    assert groups.pending[key] == (partial, 40)
    assert groups.refresh_jobs == {key}
    groups.invalidate(0)
    assert key not in groups.pending
    assert key not in groups.refresh_jobs
    host.close()


def test_invalidated_overview_metadata_rescan_yields_in_small_slices(qapp, monkeypatch):
    from prism.widgets import asset_canvas as module
    parent=QtWidgets.QWidget();parent.mode='all'
    host=AssetCanvas(parent);model=AssetModel();host.setModel(model)
    model.replace([SimpleNamespace(crop=QtCore.QRectF(0,0,120,100),_image_size=QtCore.QSize(120,100),_canvas_id='a') for _ in range(100)])
    host._invalidate_overview_span()
    ticks=iter([0,.003,.006])
    with monkeypatch.context() as patch:
        patch.setattr(module.time,'perf_counter',lambda:next(ticks))
        assert not host._scan_overview_span()
    assert host._overview_scan_cursor==2
    assert host._scan_overview_span()
    assert host._overview_pixel_span>0
    parent.close()


def test_decode_refresh_invalidates_only_changed_image_regions(qapp, monkeypatch):
    parent=QtWidgets.QWidget();parent.mode='all'
    host=AssetCanvas(parent);model=AssetModel();host.setModel(model)
    model.replace([SimpleNamespace(crop=QtCore.QRectF(0,0,120,100),_image_size=QtCore.QSize(120,100),_canvas_id='a') for _ in range(2)])
    invalidate=Mock()
    monkeypatch.setattr(host._tiles,'invalidate',invalidate)
    monkeypatch.setattr(host,'queue_visible_details',lambda:True)
    host.request_display_refresh(model.items[0])
    host._publish_display()
    invalidate.assert_called_once_with([host.cards[0].sceneBoundingRect()])
    host.wake_display_queue()
    host._publish_display()
    assert invalidate.call_count==1, 'Queue wakeups without new pixels must not dirty the viewport'
    host._display_timer.stop()
    parent.close()


def test_ready_image_publishes_while_other_visible_images_are_pending(qapp, monkeypatch):
    parent = QtWidgets.QWidget()
    parent.mode = 'all'
    host = AssetCanvas(parent)
    model = AssetModel()
    host.setModel(model)
    model.replace([SimpleNamespace(crop=QtCore.QRectF(0, 0, 120, 100),
        _image_size=QtCore.QSize(120, 100), _canvas_id='a') for _ in range(2)])
    invalidate = Mock()
    monkeypatch.setattr(host._tiles, 'invalidate', invalidate)
    monkeypatch.setattr(host, 'queue_visible_details', lambda: False)
    # A whole-view retry remains outstanding, but one image really completed.
    host.request_display_refresh()
    host.request_display_refresh(model.items[0])
    host._publish_display()
    invalidate.assert_called_once_with([host.cards[0].sceneBoundingRect()])
    assert 0 not in host._display_dirty
    assert -1 in host._display_dirty
    assert host._display_timer.isActive()
    host._display_timer.stop()
    parent.close()
