"""T3：文档里的图片必须按原样存进工程。

用户的硬要求是「导入什么导出就该是什么，不能降低质量」，任务书 T3 里又
把这件事写死了：「图片原始附件和文档中的预览图应分开保存，不能为了缩小
预览而覆盖原图。」

所以这里钉住两件容易搞混的事：

* 进 ``scene.attachments`` 的必须是**用户那份文件的字节**，插进去一张
  JPEG，存下来的还得是那张 JPEG；
* 编辑器要显示的小尺寸副本只能待在临时目录里，永远不能倒灌回附件表。

这些测试不需要 QtWebEngine：面板在页面没起来的时候照样收图、存图。
"""
import base64
import io
import os

import pytest
from PyQt6 import QtGui, QtWidgets

from prism.widgets import document_panel as module
from prism.widgets.document_panel import MAX_ATTACHMENT_EDGE, DocumentPanel


@pytest.fixture
def panel(view):
    panel = DocumentPanel(view)
    yield panel
    panel._save_timer.stop()
    panel.deleteLater()


def _photo_bytes(quality=95):
    """A real JPEG，重编码一次字节就会变，所以它能把降质量测出来。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (64, 48), (200, 30, 30)).save(
        buffer, 'JPEG', quality=quality)
    return buffer.getvalue()


def _png_bytes(edge=3):
    image = QtGui.QImage(edge, edge, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    buffer = _buffer(image, 'PNG')
    return buffer


def _big_png(edge=2500):
    """一张比编辑器需要显示的尺寸大得多的 PNG。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (edge, edge), 'green').save(buffer, 'PNG')
    return buffer.getvalue()


def _buffer(image, fmt='PNG'):
    from PyQt6 import QtCore

    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, fmt)
    return bytes(buffer.data())


# ── Storing what the user brought in ─────────────────────────────


def test_inserting_a_file_keeps_the_bytes_on_disk(panel, view, tmp_path,
                                                  monkeypatch):
    view.scene.attachments.clear()
    blob = _photo_bytes()
    source = tmp_path / 'photo.jpg'
    source.write_bytes(blob)
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getOpenFileNames',
        staticmethod(lambda *a, **k: ([str(source)], '')))
    monkeypatch.setattr(panel, '_apply', lambda *a, **k: None)

    panel.insert_image_files()

    assert len(view.scene.attachments) == 1
    name, mime, stored = view.scene.attachments[1]
    assert stored == blob               # 一模一样的字节，没有重编码
    assert mime == 'image/jpeg'         # 也没有被改名叫 PNG
    assert name == 'photo.jpg'


def test_a_pasted_picture_keeps_its_bytes(panel, view):
    view.scene.attachments.clear()
    blob = _photo_bytes()
    payload = base64.b64encode(blob).decode('ascii')

    url = panel._store_image_payload('screenshot.jpg', 'image/jpeg', payload)

    assert url
    _name, mime, stored = view.scene.attachments[1]
    assert stored == blob
    assert mime == 'image/jpeg'


def test_an_inline_data_image_keeps_its_bytes(panel, view):
    view.scene.attachments.clear()
    blob = _photo_bytes()
    payload = base64.b64encode(blob).decode('ascii')
    html = f'<p><img src="data:image/jpeg;base64,{payload}"></p>'

    result = panel._canonical_html(html)

    assert 'prism://attach/1' in result
    assert 'data:' not in result
    _name, mime, stored = view.scene.attachments[1]
    assert stored == blob
    assert mime == 'image/jpeg'


def test_a_picture_is_stored_in_full_however_large(panel, view):
    """用户选了「存原图，不设上限」，那就真的不缩。"""
    view.scene.attachments.clear()
    blob = _big_png(3000)

    attachment_id = panel._store_picture('huge.png', blob, 'image/png')

    assert view.scene.attachments[attachment_id][2] == blob


def test_a_previously_shrinking_size_is_not_applied_any_more(panel, view):
    """回归：存储路径曾经无条件把图缩到 2000px 再转 PNG。"""
    view.scene.attachments.clear()
    blob = _photo_bytes()
    attachment_id = panel._store_picture('photo.jpg', blob, 'image/jpeg')
    _name, mime, stored = view.scene.attachments[attachment_id]

    assert stored == blob
    assert mime != 'image/png'


def test_an_empty_payload_is_ignored(panel, view):
    view.scene.attachments.clear()
    assert panel._store_image_payload('x.png', 'image/png', '') is None
    assert view.scene.attachments == {}


def test_malformed_base64_is_ignored(panel, view):
    view.scene.attachments.clear()
    assert panel._store_image_payload('x.png', 'image/png', '!!!') is None
    assert view.scene.attachments == {}


# ── The small copy the editor shows ──────────────────────────────


def test_a_picture_the_editor_can_show_is_staged_untouched(panel, view):
    view.scene.attachments.clear()
    blob = _png_bytes(120)
    view.scene.attachments[1] = ('small.png', 'image/png', blob)

    panel._stage_attachment(1, view.scene.attachments[1])

    with open(os.path.join(panel._scratch, '1.png'), 'rb') as handle:
        assert handle.read() == blob


def test_a_picture_larger_than_the_editor_draws_is_copied_small(panel, view):
    from PIL import Image

    view.scene.attachments.clear()
    blob = _big_png(2500)
    view.scene.attachments[1] = ('huge.png', 'image/png', blob)

    panel._stage_attachment(1, view.scene.attachments[1])

    with Image.open(os.path.join(panel._scratch, '1.png')) as image:
        assert max(image.size) <= MAX_ATTACHMENT_EDGE
        assert image.size[0] > 1
    # 关键的半边：附件表里那张还是原图。
    assert view.scene.attachments[1][2] == blob


def test_the_shown_copy_never_travels_back_into_the_note(panel, view):
    """显示用的是临时目录里的副本，存回来的必须还是 prism:// 引用。"""
    view.scene.attachments.clear()
    blob = _big_png(2500)
    view.scene.attachments[1] = ('huge.png', 'image/png', blob)

    display = panel._display_html('<img src="prism://attach/1">')
    assert display.startswith('<img src="file:///')
    assert panel._canonical_html(display) == '<img src="prism://attach/1">'
    assert view.scene.attachments[1][2] == blob


def test_the_size_check_reads_the_header_not_the_pixels():
    assert module._larger_than_editor_draws(_big_png(2001)) is True
    assert module._larger_than_editor_draws(_big_png(2000)) is False
    assert module._larger_than_editor_draws(_photo_bytes()) is False
    assert module._larger_than_editor_draws(b'not an image') is False
    assert module._larger_than_editor_draws(b'') is False


# ── Canvas items ─────────────────────────────────────────────────


def test_a_canvas_item_is_inserted_from_its_imported_bytes(panel, view, qapp):
    from prism.items import PrismPixmapItem

    view.scene.attachments.clear()
    blob = _photo_bytes()
    item = PrismPixmapItem(
        QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32))
    item.set_source_blob(blob, 'jpg')

    attachment_id = panel._canvas_attachment(item, 'photo.jpg')

    assert view.scene.attachments[attachment_id][2] == blob


def test_a_canvas_item_without_a_file_is_painted_to_png(panel, view, qapp):
    from prism.items import PrismPixmapItem

    view.scene.attachments.clear()
    image = QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    item = PrismPixmapItem(image)

    attachment_id = panel._canvas_attachment(item, 'canvas.png')

    stored = view.scene.attachments[attachment_id][2]
    assert stored.startswith(b'\x89PNG')
    assert view.scene.attachments[attachment_id][1] == 'image/png'


def test_an_unreadable_file_is_skipped(panel, view, tmp_path, monkeypatch):
    view.scene.attachments.clear()
    missing = tmp_path / 'gone.png'
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getOpenFileNames',
        staticmethod(lambda *a, **k: ([str(missing)], '')))
    applied = []
    monkeypatch.setattr(panel, '_apply', lambda *a, **k: applied.append(a))

    panel.insert_image_files()

    assert view.scene.attachments == {}
    assert applied == []
