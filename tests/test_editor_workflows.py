from PyQt6 import QtGui

from prism.widgets.document_panel import DocumentPanel
from prism.items import PrismPixmapItem


def test_document_revisit_keeps_content_without_reloading(view):
    """Revisiting the open document must neither lose nor reload it.

    The panel used to decide "nothing changed, skip" without ever registering
    the pictures the note referred to (they never painted), and the opposite
    mistake - reloading on every visit - throws away the undo stack and the
    caret. Both halves are pinned down here; the editor page itself is not
    involved, so this runs without Chromium.
    """
    view.scene.note_html = '<p>hello</p>'
    panel = DocumentPanel(view)
    assert not panel._content_loaded
    panel.load_from_scene()
    assert panel._content_loaded
    assert panel.html() == '<p>hello</p>'
    # Second visit, nothing changed: not stale, so the page is left alone.
    panel.load_from_scene()
    assert not panel._page_stale
    # An edit that lands in the board keeps the panel and the board in step.
    panel.set_html('<p>hello world</p>')
    panel.store_to_scene()
    assert view.scene.note_html == '<p>hello world</p>'
    panel._save_timer.stop()
    panel.deleteLater()


def test_document_import_reaches_the_board_without_a_loaded_page(view):
    """The Word importer drives setHtml() then store_to_scene() at once."""
    panel = DocumentPanel(view)
    assert not panel._content_loaded
    panel.editor.setHtml('<p>imported</p>')
    panel.store_to_scene()
    assert view.scene.note_html == '<p>imported</p>'
    panel._save_timer.stop()
    panel.deleteLater()


def test_tag_filters_do_not_edit_metadata(view):
    # 「同时包含 / 任意包含」下拉 2026-10-01 随红框删除，这个测试原来靠它
    # 切到 any-of 证明"筛选不动数据"。现在筛选用默认（同时包含）语义，
    # 要验的仍然是同一件事：筛选只改可见性，不碰 item 上的标签。
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    item._tags = ['night']
    view.scene.addItem(item)
    panel = view.category_panel
    panel._active_filter = None
    panel._active_tags = {'night', 'rain'}
    panel._apply_filter()
    assert not item.isVisible()
    assert item._tags == ['night']
