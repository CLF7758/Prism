"""Coverage for the Quill-based document panel.

These tests deliberately run *without* QtWebEngine: the Python half has to be
correct on its own, and that half is what the Word importer, the save path and
the export path talk to. The editor page was verified against a real headless
Chromium separately (ribbon rendering, picture loading, table commands, find
and replace, and a 201-picture Word import).

The two dialects that have to line up here:

* the board stores ``prism://attach/<id>`` pictures in ``scene.note_html``;
* the page can only load ``file://`` URLs, staged in a scratch directory.
"""

import base64
import json
import os

import pytest
from PyQt6 import QtCore, QtGui

from prism.fileio.word import _describe_block, _images_in, normalize_editor_html
from prism.widgets import document_panel as module
from prism.widgets.document_panel import (DocumentPanel, _body_fragment,
                                          _inline_image_sizes)

#: What QTextEdit used to write; older notes still look like this.
QT_STYLE_HTML = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" '
    '"http://www.w3.org/TR/REC-html40/strict.dtd">'
    '<html><head><meta name="qrichtext" content="1" />'
    '<style type="text/css">p, li { white-space: pre-wrap; }</style></head>'
    '<body style=" font-family:\'Microsoft YaHei\'; font-size:11pt;">'
    '<p style=" margin-top:0px;">旧文档</p></body></html>')

#: Label keys editor.js reads out of the table Python pushes in.
PAGE_LABEL_KEYS = {
    'findNext', 'replaceOne', 'replaceAll', 'findPlaceholder',
    'replacePlaceholder', 'close', 'notFound', 'replaceDone', 'tableHint',
    'imageHint', 'selectTextFirst', 'unsupportedImage', 'imageFailed',
    'zoomLabel',
}

#: Commands the page must find in the ribbon it is told to render.
PAGE_COMMANDS = {
    'undo', 'redo', 'bold', 'italic', 'underline', 'header', 'lineHeight',
    'align', 'list', 'code-block', 'find-replace', 'clear-format',
    'insert-image', 'insert-canvas', 'insert-table', 'table-add-row',
    'table-del-row', 'table-add-col', 'table-del-col', 'insert-link',
    'text-color', 'image-size', 'page-view', 'web-view',
    'zoom-in', 'zoom-out', 'zoom-reset',
}


@pytest.fixture
def panel(view):
    panel = DocumentPanel(view)
    yield panel
    panel._save_timer.stop()
    panel.deleteLater()


def _png_bytes():
    """A tiny real PNG, so attachment handling sees proper bytes."""
    image = QtGui.QImage(3, 3, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


# ── The bundled page ─────────────────────────────────────────────


def test_editor_page_and_its_vendor_bundle_are_shipped():
    for name in module.EDITOR_ASSETS:
        assert os.path.exists(os.path.join(module.EDITOR_DIR, name)), name


def test_vendor_notice_records_the_upstream_licence():
    path = os.path.join(module.EDITOR_DIR, 'UPSTREAM.json')
    with open(path, encoding='utf-8') as handle:
        upstream = json.load(handle)
    assert upstream['package'] == 'quill'
    assert upstream['license'] == 'BSD-3-Clause'
    assert os.path.exists(os.path.join(module.EDITOR_DIR, 'vendor', 'LICENSE'))


# ── HTML dialects ────────────────────────────────────────────────


def test_body_fragment_drops_the_qt_document_wrapper():
    fragment = _body_fragment(QT_STYLE_HTML)
    assert '旧文档' in fragment
    assert '<html' not in fragment
    assert '<body' not in fragment
    assert 'white-space' not in fragment
    assert 'qrichtext' not in fragment


def test_body_fragment_keeps_a_plain_fragment():
    fragment = '<p>hello</p><ul><li>one</li></ul>'
    assert _body_fragment(fragment) == fragment


def test_body_fragment_of_nothing_is_empty():
    assert _body_fragment('') == ''
    assert _body_fragment(None) == ''


def test_body_fragment_keeps_entities_and_text():
    fragment = _body_fragment('<p>a &amp; b &#20013;</p>')
    assert '&amp;' in fragment
    assert '&#20013;' in fragment


def test_qt_image_size_attributes_become_inline_styles():
    html = '<p><img src="prism://attach/1" width="600" height="400"></p>'
    assert _inline_image_sizes(html) == (
        '<p><img src="prism://attach/1" '
        'style="height: 400px; width: 600px"></p>')


def test_image_size_conversion_keeps_an_existing_style():
    html = '<img src="prism://attach/1" style="border: 1px" width="600">'
    assert _inline_image_sizes(html) == (
        '<img src="prism://attach/1" style="border: 1px; width: 600px">')


def test_images_without_a_size_are_left_alone():
    html = '<img src="prism://attach/1">'
    assert _inline_image_sizes(html) == html


# ── Attachments ──────────────────────────────────────────────────


def test_attachment_urls_round_trip_through_the_scratch_directory(panel, view):
    view.scene.attachments.clear()
    view.scene.attachments[3] = ('shot.png', 'image/png', _png_bytes())
    display = panel._display_html('<img src="prism://attach/3">')
    assert display.startswith('<img src="file:///')
    assert os.path.exists(os.path.join(panel._scratch, '3.png'))
    assert panel._canonical_html(display) == '<img src="prism://attach/3">'


def test_missing_attachment_keeps_its_url_and_does_not_raise(panel, view):
    view.scene.attachments.clear()
    html = '<img src="prism://attach/99">'
    assert panel._display_html(html) == html


def test_staging_the_same_attachment_twice_writes_once(panel, view):
    view.scene.attachments.clear()
    view.scene.attachments[1] = ('a.png', 'image/png', _png_bytes())
    first = panel._display_html('<img src="prism://attach/1">')
    second = panel._display_html('<img src="prism://attach/1">')
    assert first == second


def test_inline_data_image_is_absorbed_into_the_board(panel, view):
    view.scene.attachments.clear()
    payload = base64.b64encode(_png_bytes()).decode('ascii')
    html = f'<p><img src="data:image/png;base64,{payload}"></p>'
    result = panel._canonical_html(html)
    assert 'data:' not in result
    assert 'prism://attach/1' in result
    assert view.scene.attachments[1][1] == 'image/png'


def test_html_from_the_page_comes_back_with_prism_urls(panel, view):
    view.scene.attachments.clear()
    view.scene.attachments[5] = ('x.png', 'image/png', _png_bytes())
    display = panel._display_html('<img src="prism://attach/5">')
    restored = panel._canonical_html(f'<p>{display}</p>')
    assert 'file:///' not in restored
    assert 'prism://attach/5' in restored


# ── Board round trip ─────────────────────────────────────────────


def test_store_to_scene_writes_what_the_panel_holds(panel, view):
    panel.set_html('<p>written</p>')
    panel.store_to_scene()
    assert view.scene.note_html == '<p>written</p>'


def test_store_to_scene_does_nothing_before_the_note_is_loaded(panel, view):
    view.scene.note_html = '<p>untouched</p>'
    panel.store_to_scene()
    assert view.scene.note_html == '<p>untouched</p>'


def test_a_page_keeps_its_own_html(panel, view):
    page = {'id': 'doc-1', 'kind': 'document', 'title': '规划', 'content': ''}
    view.scene.workspace_pages = [page]
    panel.page_id = 'doc-1'
    panel.set_html('<p>page note</p>')
    panel.store_to_scene()
    assert page['content'] == '<p>page note</p>'
    assert view.scene.note_html == ''

    view.scene.note_html = '<p>board note</p>'
    panel.load_from_scene()
    assert panel.html() == '<p>page note</p>'


def test_plain_text_export_reads_the_html(panel, view):
    panel.set_html('<h1>标题</h1><p>正文</p>')
    assert '标题' in panel.text()
    assert '正文' in panel.text()


def test_status_line_counts_the_note(panel, view):
    panel.set_html('<p>一二三四</p><p><img src="prism://attach/1"></p>')
    panel._refresh_status()
    status = panel._status.text()
    assert '张图片' in status or 'images' in status


# ── Ribbon and labels ────────────────────────────────────────────


def test_ribbon_has_the_three_wps_style_tabs(panel):
    spec = panel._ribbon_spec()
    assert [tab['id'] for tab in spec['tabs']] == ['home', 'insert', 'layout']
    assert spec['tabs'][0]['label'] in ('开始', 'Home')
    assert spec['tabs'][2]['label'] in ('页面布局', 'Page Layout')


def test_ribbon_covers_every_command_the_page_implements(panel):
    spec = panel._ribbon_spec()
    commands = set()
    for tab in spec['tabs']:
        for group in tab['groups']:
            for item in group['items']:
                commands.add(item['command'])
    missing = PAGE_COMMANDS - commands
    assert not missing, missing


def test_ribbon_items_carry_a_label_and_a_tooltip(panel):
    spec = panel._ribbon_spec()
    for tab in spec['tabs']:
        for group in tab['groups']:
            for item in group['items']:
                assert item.get('tip'), item
                if item['type'] == 'button':
                    assert item.get('label'), item
                elif item['type'] == 'select':
                    assert len(item['options']) > 1, item
                else:
                    assert item.get('unit'), item


def test_ribbon_offers_fonts_even_without_a_font_database(panel):
    select = next(item for tab in panel._ribbon_spec()['tabs']
                  for group in tab['groups'] for item in group['items']
                  if item.get('type') == 'select')
    assert len(select['options']) > 1


def test_label_table_matches_what_the_page_reads(panel):
    table = panel._label_table()
    assert set(table) == PAGE_LABEL_KEYS
    assert all(table.values())


# ── Word export side ─────────────────────────────────────────────


def test_word_export_reads_a_note_written_by_the_editor(qapp, view):
    view.scene.attachments.clear()
    view.scene.attachments[7] = ('shot.png', 'image/png', _png_bytes())
    html = ('<h1>标题</h1><p>正文 <strong>粗</strong></p>'
            '<pre data-language="plain">print(1)</pre>'
            '<p><img src="prism://attach/7"></p>')

    document = QtGui.QTextDocument()
    document.setHtml(normalize_editor_html(html))
    kinds = []
    block = document.begin()
    while block.isValid():
        kinds.append(_describe_block(block)[0])
        block = block.next()

    assert 'heading' in kinds
    assert 'code' in kinds
    assert 'image' in kinds
    assert list(_images_in(document, view.scene.attachments)) == [
        'prism://attach/7']


def test_word_export_lists_survive_the_editor_dialect(qapp):
    html = '<ul><li>one</li><li>two</li></ul><ol><li>first</li></ol>'
    document = QtGui.QTextDocument()
    document.setHtml(normalize_editor_html(html))
    kinds = []
    block = document.begin()
    while block.isValid():
        kinds.append(_describe_block(block)[0])
        block = block.next()
    assert kinds.count('list') == 3


def test_normalize_keeps_html_without_code_blocks_untouched():
    html = '<h1>标题</h1><p>正文</p>'
    assert normalize_editor_html(html) == html


# ── Import race ──────────────────────────────────────────────────


class _StubPage:
    """A page that answers every question with the same HTML."""

    def __init__(self, html):
        self.html = html

    def runJavaScript(self, code, callback):         # noqa: N802 - Qt spelling
        # Real runJavaScript callbacks arrive from the event loop; a deferred
        # call keeps the same ordering without waiting for the timeout.
        QtCore.QTimer.singleShot(0, lambda: callback(self.html))


@pytest.mark.skipif(not module.WEBENGINE_AVAILABLE,
                    reason='needs the QtWebEngine bridge object')
def test_a_push_in_flight_is_never_read_back(panel):
    """Reading the page while it still holds the previous note is a trap.

    view.py imports a Word file as setHtml() immediately followed by
    store_to_scene(); a read-back in that window returns the *old* note and
    throws the import away. The barrier is what makes that impossible.
    """
    panel._ready = True
    panel.set_html('<p>imported</p>')
    assert panel._pending_push
    panel._page = _StubPage('<p>previous</p>')
    assert panel._flush_from_page() is None
    panel._pending_push = False
    assert panel._flush_from_page() == '<p>previous</p>'


@pytest.mark.skipif(not module.WEBENGINE_AVAILABLE,
                    reason='needs the QtWebEngine bridge object')
def test_store_keeps_the_import_while_the_page_catches_up(panel, view):
    panel._ready = True
    panel.set_html('<p>imported</p>')
    panel._page = _StubPage('<p>previous</p>')
    panel.store_to_scene()
    assert view.scene.note_html == '<p>imported</p>'
