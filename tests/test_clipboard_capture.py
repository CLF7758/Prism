"""T2.5：无浏览器插件的剪贴板素材采集。

分两层测：Adapter 和 Service 是纯逻辑，不需要窗口；集成部分用 view
fixture 验证采集到的东西真的能进画布，并且原始字节一路保住。

剪贴板本身在这组测试里只碰一次 —— QClipboard.setMimeData() 会把
QMimeData 的所有权交给 Qt，反复设置容易触发双重释放，所以 Service
的规则直接喂快照来测。
"""
import hashlib
import time

from PyQt6 import QtCore, QtGui

from prism.clipboard_capture import (ClipboardCaptureAdapter,
                                     ClipboardCaptureService,
                                     ClipboardSnapshot,
                                     download_url_snapshot,
                                     http_url_from_mime,
                                     snapshot_from_mime)


def _png(colour=(200, 80, 40), size=(80, 60)):
    image = QtGui.QImage(size[0], size[1], QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(*colour))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


def _snapshot(data, suffix='png'):
    return ClipboardSnapshot(data, suffix, f'image/{suffix}', False)


# ── Service ────────────────────────────────────────────────────────────

def test_snapshot_keeps_original_bytes_and_digest(qapp):
    data = _png()
    snapshot = _snapshot(data)

    assert snapshot.data == data
    assert snapshot.digest == hashlib.sha256(data).hexdigest()
    assert snapshot.suffix == 'png'


def test_capture_lands_in_the_inbox(qapp):
    service = ClipboardCaptureService()
    service.accept(_snapshot(_png()))

    assert len(service) == 1
    assert service.items[0].preview is not None


def test_the_same_image_is_not_captured_twice(qapp):
    """内容相同的第二次复制只提升顺序，不新建条目。"""
    service = ClipboardCaptureService()
    data = _png()
    service.accept(_snapshot(data))
    service.accept(_snapshot(_png((40, 160, 90))))
    first_id = service.items[1].digest

    service.accept(_snapshot(data))

    assert len(service) == 2, '重复内容不该产生第三条'
    assert service.ignored == 1
    assert service.items[0].digest == first_id, '重复项应当被提到最前'


def test_different_content_is_a_new_capture(qapp):
    service = ClipboardCaptureService()
    service.accept(_snapshot(_png((10, 20, 30))))
    service.accept(_snapshot(_png((200, 100, 50))))
    assert len(service) == 2


def test_inbox_respects_its_limit(qapp):
    service = ClipboardCaptureService(max_items=3)
    for index in range(6):
        service.accept(_snapshot(_png((index * 40, 20, 30))))
    assert len(service) == 3, '超出上限应当挤掉最旧的'


def test_take_removes_entries(qapp):
    service = ClipboardCaptureService()
    service.accept(_snapshot(_png()))
    service.accept(_snapshot(_png((90, 90, 90))))

    taken = service.take(list(service.items))

    assert len(taken) == 2
    assert len(service) == 0


def test_clear_empties_the_inbox(qapp):
    service = ClipboardCaptureService()
    service.accept(_snapshot(_png()))
    service.clear()
    assert len(service) == 0


def test_expired_entries_are_dropped(qapp):
    """超过保留时间的条目在下次接收时被清掉。"""
    service = ClipboardCaptureService(max_age_hours=0.0001)
    service.accept(_snapshot(_png()))
    assert len(service) == 1
    time.sleep(0.5)
    service.accept(_snapshot(_png((7, 7, 7))))
    assert len(service) == 1, '过期的旧条目应当已被清理'


# ── 边界 ───────────────────────────────────────────────────────────────

def test_snapshot_ignores_missing_and_non_image_mime(qapp):
    assert snapshot_from_mime(None) is None
    assert snapshot_from_mime(QtCore.QMimeData()) is None

    text_only = QtCore.QMimeData()
    text_only.setText('just words')
    assert snapshot_from_mime(text_only) is None, \
        '纯文本默认不进素材库'


def test_snapshot_prefers_original_mime_over_re_encoding(qapp):
    data = _png()
    mime = QtCore.QMimeData()
    mime.setData('image/png', data)

    snapshot = snapshot_from_mime(mime)

    assert snapshot is not None
    assert snapshot.data == data, '应当直接取原始编码字节'
    assert snapshot.source_format == 'image/png'


def test_http_url_is_recognised_but_ordinary_text_is_not(qapp):
    mime = QtCore.QMimeData()
    mime.setText('https://example.com/reference.png')
    assert http_url_from_mime(mime) == 'https://example.com/reference.png'
    mime.setText('notes for later')
    assert http_url_from_mime(mime) is None


def test_url_download_preserves_response_bytes_and_source(qapp):
    payload = _png((20, 90, 180))

    class Headers(dict):
        def get_content_type(self):
            return 'image/png'

    class Response:
        headers = Headers()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, _limit):
            return payload

    snapshot = download_url_snapshot(
        'https://example.com/reference.png', opener=lambda *_a, **_k: Response())
    assert snapshot.data == payload
    assert snapshot.source_url == 'https://example.com/reference.png'
    assert snapshot.suffix == 'png'


def test_url_download_rejects_non_images(qapp):
    class Response:
        headers = {'Content-Type': 'text/html'}
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, _limit):
            return b'<html>not an image</html>'

    import pytest
    with pytest.raises(ValueError, match='supported image'):
        download_url_snapshot(
            'https://example.com/page', opener=lambda *_a, **_k: Response())


# ── Adapter ────────────────────────────────────────────────────────────

def test_adapter_reports_a_capture(qapp):
    """通过真实剪贴板走一次，确认 Adapter 能收到。"""
    clipboard = QtGui.QGuiApplication.clipboard()
    data = _png((123, 45, 67))

    received = []
    adapter = ClipboardCaptureAdapter(enabled=True)
    adapter.snapshot_ready.connect(received.append)
    adapter.start()
    try:
        mime = QtCore.QMimeData()
        mime.setData('image/png', data)
        # setMimeData 把所有权交给 Qt，这里之后不再引用 mime
        clipboard.setMimeData(mime)

        deadline = time.time() + 2.0
        while time.time() < deadline and not received:
            qapp.processEvents()
            time.sleep(0.01)
    finally:
        adapter.stop()

    assert received, '防抖之后应当收到一次快照'
    assert received[0].data == data, '收到的应是原始字节'


def test_adapter_can_be_switched_off(qapp):
    adapter = ClipboardCaptureAdapter(enabled=True)
    adapter.set_enabled(False)
    assert adapter.enabled is False
    adapter.set_enabled(True)
    assert adapter.enabled is True


# ── 集成：接入 view 之后 ────────────────────────────────────────────────

def test_view_owns_an_inbox_and_an_adapter(view):
    assert hasattr(view, 'clipboard_inbox')
    assert hasattr(view, 'clipboard_adapter')
    assert len(view.clipboard_inbox) == 0


def test_filing_a_capture_puts_it_on_the_canvas(view):
    """导入到画布之后，素材必须保住剪贴板里的原始字节。

    这里按「导入前后多出来的那个 item」来找，不按标题找 —— 测试环境的
    english_ui_strings fixture 会清空翻译表，标题不是稳定的判断依据。
    """
    data = _png((60, 130, 190))
    view.clipboard_inbox.accept(_snapshot(data))

    before = {id(i) for i in view.scene.items() if hasattr(i, 'save_id')}
    view._on_inbox_import(list(view.clipboard_inbox.items))
    added = [i for i in view.scene.items()
             if hasattr(i, 'save_id') and id(i) not in before]

    assert len(added) == 1
    item = added[0]
    assert item.has_original_source(), '剪贴板素材应当保留原始字节'
    assert hashlib.sha256(item.original_bytes()).hexdigest() == \
        hashlib.sha256(data).hexdigest()


def test_empty_selection_is_reported_not_crashed(view):
    """没有素材时导入应当只是没效果，不能抛异常。"""
    before = len([i for i in view.scene.items() if hasattr(i, 'save_id')])
    view._on_inbox_import([])
    after = len([i for i in view.scene.items() if hasattr(i, 'save_id')])
    assert after == before
