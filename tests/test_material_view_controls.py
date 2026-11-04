from pathlib import Path
import io
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from PyQt6 import QtCore,QtGui,QtWidgets
from prism.material_textures import prepare_texture,prepare_render
from prism.widgets.material_render import MaterialRender


def js(viewer,expression,qtbot):
    result=[]
    viewer.web.page().runJavaScript(expression,result.append)
    qtbot.waitUntil(lambda: bool(result),timeout=10000)
    return result[0]


def test_every_channel_and_shape_render(qtbot,tmp_path):
    if QtWidgets.QApplication.platformName()!='windows':pytest.skip('native WebGL')
    file=tmp_path/'gray.png';Image.new('L',(64,64),130).save(file)
    info,blob=prepare_texture(file)
    textures={slot:dict(info,attachmentId=1,channel='R') for slot in ('roughness','metallic','ao','opacity','sss','displacement')}
    payload=prepare_render(dict(textures=textures,preview={}),{1:(info['name'],info['mime'],blob)},tmp_path/'inputs')
    viewer=MaterialRender();qtbot.addWidget(viewer);viewer.resize(600,600)
    complete=[];errors=[];viewer.completed.connect(lambda *args:complete.append(args));viewer.failed.connect(errors.append)
    viewer.show();viewer.load_material(payload,'test')
    qtbot.waitUntil(lambda: bool(complete or errors),timeout=45000)
    assert not errors,errors
    output=Path('artifacts/material-tests');output.mkdir(parents=True,exist_ok=True)
    for slot in textures:
        error=js(viewer,"(()=>{try{prismRender.view("+repr(slot)+");return ''}catch(e){return String(e)}})()",qtbot)
        assert error=='',error
        qtbot.waitUntil(lambda: js(viewer,'debugMaterial.isReady(sphere)',qtbot),timeout=15000)
        qtbot.wait(400)
        image=viewer.web.grab().toImage()
        assert not image.isNull()
        center=image.pixelColor(image.width()//2,image.height()//2)
        assert abs(center.red()-130)<8,(slot,center.getRgb())
        image.save(str(output/('channel-'+slot+'.png')))
    for shape in ('sphere','cylinder','box','plane'):
        viewer.set_shape(shape);qtbot.wait(200)
        assert js(viewer,'previewShape',qtbot)==shape
        assert js(viewer,'sphere.getTotalVertices()>0',qtbot)
        viewer.web.grab().save(str(output/('shape-'+shape+'.png')))


def test_normal_flip_only_green(main_window,tmp_path):
    from prism.material_textures import transform_image
    result=transform_image(Image.new('RGB',(1,1),(80,90,230)),'normal',{'slots':{'normal':{'invert':True}}})
    assert result.getpixel((0,0))==(80,165,230)


def test_drop_on_filename_and_controls(main_window,qtbot,tmp_path):
    page=main_window.material_library;main_window.open_material_library();page.create_material()
    file=tmp_path/'drop.png';Image.new('L',(8,8),40).save(file)
    slot=page.details.slots['roughness']
    for target in (slot,slot.filename,slot.slider):
        mime=QtCore.QMimeData();mime.setUrls([QtCore.QUrl.fromLocalFile(str(file))])
        enter=QtGui.QDragEnterEvent(QtCore.QPoint(2,2),QtCore.Qt.DropAction.CopyAction,mime,QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(target,enter);assert enter.isAccepted()
        drop=QtGui.QDropEvent(QtCore.QPointF(2,2),QtCore.Qt.DropAction.CopyAction,mime,QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(target,drop);assert drop.isAccepted()
    qtbot.waitUntil(lambda:'roughness' in page.records.get(page.selected_id)['textures'])


def test_color_picker_and_reset(main_window,qtbot):
    from prism.widgets.material_color import MaterialColorDialog
    dialog=MaterialColorDialog([.2,.5,.8]);qtbot.addWidget(dialog)
    dialog.set_color(QtGui.QColor('#ff0000'))
    assert dialog.selected_rgb()==[1.,0.,0.]
    dialog.show();dialog.grab().save('artifacts/material-tests/color-picker.png')
    page=main_window.material_library;main_window.open_material_library();page.create_material()
    page.change_slot_options('basecolor',{'color':[.2,.5,.8],'value':.4})
    page.details.slots['basecolor'].reset_color.click()
    options=page.records.get(page.selected_id)['preview']['properties']['slots']['basecolor']
    assert options['color']==[1.,1.,1.] and options['value']==1


def test_displacement_midpoint_and_uniform_rows(main_window,qtbot):
    window=main_window;window.resize(1450,950);window.show();window.open_material_library()
    page=window.material_library.ensure_created();page.create_material()
    slot=page.details.slots['displacement'];slot.fold.setChecked(True)
    qtbot.wait(100)
    page.details.scroll.ensureWidgetVisible(slot.midpoint)
    qtbot.wait(100)
    assert slot.midpoint.prefix()==''
    assert slot.midpoint.fontMetrics().horizontalAdvance(slot.midpoint.text())+6 < slot.midpoint.width()
    heights={page.details.slots[key].amount.height() for key in page.details.slots}
    assert heights=={26}
    point=slot.midpoint.mapTo(page.details.scroll.viewport(),QtCore.QPoint(0,0))
    assert page.details.scroll.viewport().rect().contains(QtCore.QRect(point,slot.midpoint.size()))
    page.details.grab().save('artifacts/material-tests/displacement-control.png')


_HEADER_WIDTHS = (240, 320, 520)
_HEADER_PERCENTAGES = (100, 125, 150, 175, 200)
_LONG_HEADER_STATUS = '渲染失败：原始贴图已完整保存，请检查透明通道和法线格式后重试。' * 40 + '\n完整第二行 <b>不是富文本</b>'


def _header_record(record_id):
    return dict(id=record_id, name='布局回归材质', notes='', status='draft',
                textures={}, preview={}, files=[], tags=[])


def _settle_header(details):
    app = QtWidgets.QApplication.instance()
    app.processEvents()
    details.header.layout().activate()
    details.layout().activate()
    app.processEvents()


def _header_geometry(details):
    return (details.sections.tabBar().mapTo(details, QtCore.QPoint()).y(),
            details.header.height(), details.header.width(),
            details.render_button.geometry().getRect(),
            details.open_button.geometry().getRect(),
            details.export_button.geometry().getRect())


def _header_screen_metrics(details):
    screen = details.screen()
    return dict(platform=QtWidgets.QApplication.platformName(),
                logicalDotsPerInch=screen.logicalDotsPerInch(),
                devicePixelRatio=screen.devicePixelRatio(),
                widgetDevicePixelRatio=details.devicePixelRatioF(),
                width=details.width())


def _check_header_transitions(details, width):
    """Compare against each font/scale's own baseline, never a fixed tab y."""
    _settle_header(details)
    baseline = _header_geometry(details)
    header_minimum = details.header.minimumSizeHint().width()
    assert details.width() == details.header.width() == width
    assert isinstance(details.preview_stack, QtWidgets.QStackedWidget)
    assert details.preview_stack.count() == 2
    assert details.preview_stack.height() == 190
    assert details.preview_stack.layout().stackingMode() == QtWidgets.QStackedLayout.StackingMode.StackOne
    assert details.header.layout().indexOf(details.preview_stack) >= 0
    assert details.header.layout().indexOf(details.preview) == -1
    assert details.header.layout().indexOf(details.inline_render) == -1
    labels = (details.operation_status, details.preview_status, details.auto_mode)
    heights = tuple(label.height() for label in labels)

    def unchanged():
        _settle_header(details)
        assert _header_geometry(details) == baseline
        assert details.header.minimumSizeHint().width() == header_minimum
        assert tuple(label.height() for label in labels) == heights
        assert details.width() == details.header.width() == width
        assert details.preview_stack.height() == 190

    for label in labels:
        label.setText(_LONG_HEADER_STATUS)
        unchanged()
        assert label.text() == label.toolTip() == _LONG_HEADER_STATUS
        assert not label.wordWrap()
        assert label.textFormat() == QtCore.Qt.TextFormat.PlainText
        assert not label.hasHeightForWidth()
        assert label.sizePolicy().retainSizeWhenHidden()
        assert label.minimumSizeHint().width() == 0
        elided = label._elided_text()
        assert elided.endswith('\u2026')
        assert '\n' not in elided
        assert label.fontMetrics().horizontalAdvance(elided) <= label._text_rect().width()
        label.grab()  # Exercise paintEvent as well as the measured elision.
        label.hide()
        unchanged()
        label.clear()
        unchanged()
        assert label.text() == label.toolTip() == ''
        label.show()
        unchanged()

    assert details.render_button.sizePolicy().retainSizeWhenHidden()
    for live in (True, True, False, False, True, False):
        details.set_live_preview(live)
        unchanged()
        assert details.preview_stack.currentWidget() is (details.inline_render if live else details.preview)
        assert details.inline_render.isVisible() == live
        assert details.preview.isVisible() != live
        assert details.inline_render.height() == details.preview.height() == 190
        for retry in (True, False):
            details.render_button.setVisible(retry)
            details.operation_status.setText(_LONG_HEADER_STATUS if retry else '')
            details.preview_status.setText(_LONG_HEADER_STATUS if retry else '预览完成')
            unchanged()

    details.operation_status.setText(_LONG_HEADER_STATUS)
    details.set_live_preview(True)
    details.show_record(_header_record('second'))
    unchanged()
    assert details.operation_status.text() == details.operation_status.toolTip() == ''
    assert details.preview_stack.currentWidget() is details.preview
    details.operation_status.setText('同一记录保留操作状态')
    details.show_record(_header_record('second'))
    unchanged()
    assert details.operation_status.text() == '同一记录保留操作状态'
    details.show_record(None)
    assert details.operation_status.text() == details.operation_status.toolTip() == ''
    details.show_record(_header_record('third'))
    unchanged()
    return baseline[0]


@pytest.fixture
def material_header_details(qtbot, monkeypatch):
    from prism.widgets.material_details import MaterialDetails
    # Keep real MaterialRender visibility events and timers; skip only WebEngine.
    ensure_web = MagicMock(name='_ensure_web')
    monkeypatch.setattr(MaterialRender, '_ensure_web', ensure_web)
    details = MaterialDetails()
    qtbot.addWidget(details)
    details.show_record(_header_record('first'))
    return details, ensure_web


@pytest.mark.parametrize('width', _HEADER_WIDTHS)
@pytest.mark.parametrize('font_percent', _HEADER_PERCENTAGES)
def test_material_header_no_tab_jump(material_header_details, width, font_percent, record_property):
    details, _ensure_web = material_header_details
    font = QtGui.QFont(details.font())
    font.setPointSizeF(max(1.0, font.pointSizeF()) * font_percent / 100)
    details.setFont(font)
    details.resize(width, 1400)
    details.show()
    _settle_header(details)
    metrics = _header_screen_metrics(details)
    metrics['fontPercent'] = font_percent  # Font coverage, not simulated screen DPI.
    record_property('material_header_screen', json.dumps(metrics))
    print('material_header_screen', json.dumps(metrics))
    assert _check_header_transitions(details, width) > 0


class _HeaderEvents(QtCore.QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.types = []

    def eventFilter(self, watched, event):
        self.types.append(event.type())
        return False


@pytest.mark.parametrize('width', _HEADER_WIDTHS)
def test_material_header_native_visibility_events(material_header_details, width):
    details, ensure_web = material_header_details
    details.resize(width, 1400)
    details.show()
    _settle_header(details)
    observer = _HeaderEvents(details)
    details.inline_render.installEventFilter(observer)
    baseline = _header_geometry(details)
    calls = ensure_web.call_count
    details.set_live_preview(True)
    _settle_header(details)
    assert QtCore.QEvent.Type.Show in observer.types
    assert ensure_web.call_count == calls + 1
    details.inline_render._render_deadline.start()
    details.inline_render._capture_timer.start()
    details.set_live_preview(False)
    _settle_header(details)
    assert QtCore.QEvent.Type.Hide in observer.types
    assert not details.inline_render._render_deadline.isActive()
    assert not details.inline_render._capture_timer.isActive()
    assert _header_geometry(details) == baseline
    observer.types.clear()
    details.set_live_preview(False)
    _settle_header(details)
    assert QtCore.QEvent.Type.Show not in observer.types
    assert QtCore.QEvent.Type.Hide not in observer.types
    # Switching a hidden inspector must not spuriously show the live renderer.
    details.hide()
    observer.types.clear()
    details.set_live_preview(True)
    assert QtCore.QEvent.Type.Show not in observer.types
    details.show()
    _settle_header(details)
    assert QtCore.QEvent.Type.Show in observer.types
    details.set_live_preview(False)
    _settle_header(details)
    assert _header_geometry(details) == baseline


@pytest.mark.parametrize('width', _HEADER_WIDTHS)
def test_material_header_font_and_margins_recalculate(material_header_details, width):
    details, _ensure_web = material_header_details
    details.resize(width, 1600)
    details.show()
    _settle_header(details)
    labels = (details.operation_status, details.preview_status, details.auto_mode)
    observers = [_HeaderEvents(details) for _label in labels]
    for label, observer in zip(labels, observers):
        label.installEventFilter(observer)
        label.setContentsMargins(3, 2, 5, 4)
        assert label.minimumHeight() == label.fontMetrics().height() + 6
        label.setMargin(1)
        assert label.minimumHeight() == label.fontMetrics().height() + 8
    _settle_header(details)
    initial_heights = tuple(label.height() for label in labels)
    for label in labels:
        label.hide()
    font = QtGui.QFont(details.font())
    font.setPointSizeF(max(1.0, font.pointSizeF()) * 2)
    details.setFont(font)  # Native inherited FontChange, not a synthetic setFont loop.
    _settle_header(details)
    assert tuple(label.height() for label in labels) != initial_heights
    for label, observer in zip(labels, observers):
        assert observer.types.count(QtCore.QEvent.Type.FontChange) == 1
        margins = label.contentsMargins()
        expected = label.fontMetrics().height() + margins.top() + margins.bottom() + 2 * label.margin()
        assert label.height() == label.minimumHeight() == label.maximumHeight() == expected
        before = label.height()
        # Verify event routing only; this is NOT evidence of a native DPI change.
        QtWidgets.QApplication.sendEvent(label, QtCore.QEvent(QtCore.QEvent.Type.DevicePixelRatioChange))
        assert label.height() == before
    hidden_geometry = _header_geometry(details)
    for label in labels:
        label.show()
    _settle_header(details)
    assert _header_geometry(details) == hidden_geometry
    _check_header_transitions(details, width)


def _material_header_scale_probe(width):
    """Child-process entry: scale is configured before QApplication exists."""
    from prism.widgets.material_details import MaterialDetails
    app = QtWidgets.QApplication(['material-header-scale-probe'])
    with patch.object(MaterialRender, '_ensure_web', MagicMock(name='_ensure_web')):
        details = MaterialDetails()
        details.show_record(_header_record('first'))
        details.resize(width, 1400)
        details.show()
        _settle_header(details)
        metrics = _header_screen_metrics(details)
        metrics['tabY'] = _check_header_transitions(details, width)
        metrics['qtScaleFactor'] = float(os.environ['QT_SCALE_FACTOR'])
        print('MATERIAL_HEADER_SCALE=' + json.dumps(metrics))
        details.close()
        details.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize('width', _HEADER_WIDTHS)
@pytest.mark.parametrize('dpi_percent', _HEADER_PERCENTAGES)
def test_material_header_isolated_dpi_scale(width, dpi_percent, record_property):
    project = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    # Offscreen is deterministic regression coverage, not native Windows acceptance.
    env.update(QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR=str(dpi_percent / 100),
               QT_FONT_DPI='96', QT_SCALE_FACTOR_ROUNDING_POLICY='PassThrough',
               PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
               PYTHONPATH=str(project))
    env.pop('QT_SCREEN_SCALE_FACTORS', None)
    code = "import runpy, sys; runpy.run_path(sys.argv[1])['_material_header_scale_probe'](int(sys.argv[2]))"
    result = subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).resolve()), str(width)],
                            cwd=str(project), env=env, capture_output=True, text=True,
                            encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith('MATERIAL_HEADER_SCALE='))
    metrics = json.loads(line.split('=', 1)[1])
    record_property('material_header_screen', json.dumps(metrics))
    print('material_header_isolated_screen', json.dumps(metrics))
    assert metrics['platform'] == 'offscreen'
    assert metrics['width'] == width
    assert metrics['logicalDotsPerInch'] == pytest.approx(96)
    assert metrics['devicePixelRatio'] == pytest.approx(dpi_percent / 100)
    assert metrics['widgetDevicePixelRatio'] == pytest.approx(dpi_percent / 100)
