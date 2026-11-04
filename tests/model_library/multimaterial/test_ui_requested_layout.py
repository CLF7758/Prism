"""User-requested material-library controls, without the old debug/state UI."""
import io
from types import SimpleNamespace

from PIL import Image

from prism.widgets.model_material_picker import SceneMaterialLibrarySource


def test_only_library_slot_controls_are_visible(loaded):
    assert not loaded.parts_toggle.isVisibleTo(loaded)
    assert not loaded.sites.isVisibleTo(loaded)
    assert all(not bar.isVisibleTo(loaded) for bar in loaded.editor.bars.values())
    for widget in (loaded.editor.title,loaded.editor.notice,loaded.editor.source_note,
                   loaded.preview_status,loaded.save_status,loaded.cover_status,loaded.export_notice):
        assert not widget.isVisibleTo(loaded)
    assert loaded.editor.slots['basecolor'].isVisibleTo(loaded)


def test_normal_map_only_offers_vector_channels(loaded):
    loaded.select_material('w-wood')
    channel=loaded.editor.slots['normal'].channel
    assert channel.count()>1
    for index in range(channel.count()):
        assert channel.model().item(index).isEnabled() == (channel.itemData(index)=='RGB')


def test_setting_without_a_file_cannot_send_a_broken_request(loaded,port):
    before=len(port.calls_of('preview'))+len(port.calls_of('commit'))
    loaded._texture_requested('basecolor','set',{})
    assert len(port.calls_of('preview'))+len(port.calls_of('commit'))==before


def test_failed_edits_keep_a_context_menu_retry_without_the_old_footer(loaded,port):
    from tests.model_library.multimaterial.conftest import start_commit
    request=start_commit(loaded,port)
    assert loaded.publish_commit_failed(request.target,'write failed',request_id=request.request_id)
    assert loaded.materials.retry_available
    assert not loaded.editor.retry.isVisibleTo(loaded)


def test_picker_reads_a_real_thumbnail_past_an_unloaded_cache_entry(qapp):
    output=io.BytesIO();Image.new('RGB',(8,8),'red').save(output,format='PNG')
    blob=output.getvalue()
    class Attachments(dict):
        def read_uncached(self,key):
            assert key==7
            return ('sphere.png','image/png',blob)
    attachments=Attachments({7:('sphere.png','image/png',None)})
    scene=SimpleNamespace(material_records=[dict(id='red',name='Red',preview={'thumbnailAttachmentId':7})],
                          attachments=attachments)
    image=SceneMaterialLibrarySource(scene).thumbnail('red')
    assert image.startswith('data:image/png;base64,')
    import base64
    assert base64.b64decode(image.split(',',1)[1])==blob


def test_narrow_inspector_keeps_slot_controls_inside_the_viewport(loaded,qtbot):
    from PyQt6 import QtWidgets,QtCore
    loaded.resize(300,750)
    loaded.show()
    qtbot.wait(20)
    scroll=loaded.editor.findChild(QtWidgets.QScrollArea)
    slot=loaded.editor.slots['basecolor']
    assert scroll.viewport().width()>180
    for control in (slot.drop,slot.clear,slot.channel,slot.reuse,slot.amount,slot.color):
        position=control.mapTo(scroll.viewport(),QtCore.QPoint(0,0))
        assert position.x()>=0,(control,position)
        assert position.x()+control.width()<=scroll.viewport().width(),(control,position,scroll.viewport().size())


def test_actual_file_picker_signal_reaches_the_command(loaded,port,tmp_path,monkeypatch):
    from PyQt6 import QtWidgets
    from PIL import Image
    image=tmp_path/'texture.png';Image.new('RGB',(8,8),'red').save(image)
    monkeypatch.setattr(QtWidgets.QFileDialog,'getOpenFileName',lambda *args,**kwargs:(str(image),'PNG'))
    loaded.select_material('w-wood')
    loaded.editor.slots['basecolor'].drop.click()
    requests=port.calls_of('commit')
    assert requests
    import json
    request=requests[-1][1]
    assert json.loads(request.parameters_json)['patch']['texture']['path']==str(image)
