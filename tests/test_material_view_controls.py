from pathlib import Path
import io
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
    page=window.material_library;page.create_material()
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
