# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""Save a board document as a file the user can take away.

A note keeps its pictures in the board's attachment table and refers to them
as ``prism://attach/<id>``.  That URL means something inside Prism and
nowhere else: written into a .html file as-is it is a broken picture in every
browser there is.  The user then holds a file that looks like a web page but
shows nothing, which is exactly the sort of surprise the task book rules out
("导出为 HTML、DOCX 或 PDF 时不误导用户").  Every format a user can pick
therefore goes through here, so no export path can forget to bring the
pictures with it.

What each format does with a picture:

* ``.html`` - embedded as a ``data:`` URL, so one file holds everything and
  nothing breaks when the file is moved
* ``.pdf``  - resolved while the page is drawn, straight from the board, so
  the picture is never copied into a second buffer
* ``.docx`` - handed to :mod:`prism.fileio.word`, which passes the stored
  bytes through untouched whenever Word can take them
* ``.txt``  - no pictures at all; that is what "plain text" means

``EXPORT_FORMATS`` is the single list of what can be exported, and
:func:`filter_string` turns it into the filter a file dialog shows.  A format
cannot appear in the dialog without a writer behind it.
"""
import collections
import logging
import os
import re

from PyQt6 import QtCore, QtGui

from prism.fileio.attachments import ATTACHMENT_PREFIX, encode_data_url
from prism.i18n import _

logger = logging.getLogger(__name__)


class DocumentExportError(Exception):
    """Raised when a note cannot be written in the chosen format."""


#: One entry of :data:`EXPORT_FORMATS`.
#:
#: ``writer`` takes ``(html, attachments, target_path)`` and returns how many
#: pictures ended up in the file.
ExportFormat = collections.namedtuple(
    'ExportFormat', 'extension label writer hint')


#: ``src="prism://attach/<id>"``, with either kind of quote.
_ATTACHMENT_SRC = re.compile(
    r'src=(?P<quote>["\'])(?P<url>' + re.escape(ATTACHMENT_PREFIX)
    + r'(?P<id>\d+))(?P=quote)', re.IGNORECASE)

_IMG_TAG = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_IMG_HAS_SIZE = re.compile(r'\b(?:width|height)\s*[:=]', re.IGNORECASE)

#: Widest a picture may be drawn on a page, in CSS pixels.  A4 is 21 cm wide;
#: with 15 mm margins that leaves 18 cm, which is 680 px at 96 dpi.
MAX_PAGE_IMAGE_WIDTH = 680

#: Resolution the PDF is written at.  96 dpi is what CSS pixels mean, so the
#: sizes in the note are the sizes on the paper.
PDF_RESOLUTION = 96
#: Paper margins, in millimetres.
PDF_MARGIN_MM = 15

_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="generator" content="Prism">
<title>{title}</title>
<style>
body {{ font-family: 'Microsoft YaHei', '微软雅黑', sans-serif;
        font-size: 11pt; line-height: 1.6;
        margin: 2em auto; max-width: 46em; padding: 0 1em; color: #202124; }}
img {{ max-width: 100%; height: auto; }}
table {{ border-collapse: collapse; margin: 1em 0; }}
td, th {{ border: 1px solid #999; padding: 4px 8px; }}
pre {{ background: #f5f5f5; padding: 8px; overflow-x: auto; }}
blockquote {{ border-left: 3px solid #ccc; margin-left: 0; padding-left: 1em;
              color: #555; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


# ── Pictures ──────────────────────────────────────────────────


def html_with_embedded_images(html, attachments):
    """Replace every board picture URL with an inline ``data:`` URL.

    A reference whose picture is missing from the board is left exactly as it
    was: a broken link is honest about what happened, while deleting it would
    silently change the document.
    """
    if not html:
        return ''
    entries = attachments or {}

    def replace(match):
        identifier = int(match.group('id'))
        entry = entries.get(identifier)
        if not entry or len(entry) != 3:
            logger.info('Exporting a note that refers to missing attachment '
                        '%s', identifier)
            return match.group(0)
        _name, mime, blob = entry
        if not blob:
            return match.group(0)
        return 'src="%s"' % encode_data_url(mime or 'image/png', blob)

    return _ATTACHMENT_SRC.sub(replace, html)


def html_with_fitted_images(html, limit):
    """Give pictures without a size a width that fits the page.

    Quill stores an inserted picture without any size, so Qt would draw it at
    its natural pixel size - a 6000 px screenshot straight off the edge of an
    A4 page.  A picture that carries its own size is left alone: the user
    chose that size.
    """
    if not html or not limit or limit <= 0:
        return html or ''
    width = int(limit)

    def fix(match):
        tag = match.group(0)
        if _IMG_HAS_SIZE.search(tag):
            return tag
        stripped = tag[:-1].rstrip()
        if stripped.endswith('/'):              # an XHTML style <img ... />
            stripped = stripped[:-1].rstrip()
        return f'{stripped} width="{width}">'

    return _IMG_TAG.sub(fix, html)


# ── HTML ──────────────────────────────────────────────────────


def export_note_to_html(html, attachments, target_path):
    """Write the note as one self-contained .html file."""
    embedded = html_with_embedded_images(html, attachments)
    pictures = _count_embedded(embedded)
    title = _title_for(target_path) or _('Document')
    page = _HTML_PAGE.format(title=_escape(title), body=embedded or '<p></p>')
    try:
        with open(target_path, 'w', encoding='utf-8') as handle:
            handle.write(page)
    except OSError as exc:
        raise DocumentExportError(
            _('Could not write {path}: {reason}')
            .format(path=target_path, reason=exc)) from exc
    return pictures


def _count_embedded(html):
    return len(re.findall(r'src="data:', html, re.IGNORECASE))


def _escape(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _title_for(path):
    stem = os.path.splitext(os.path.basename(str(path or '')))[0]
    return stem


# ── PDF ───────────────────────────────────────────────────────


class _AttachmentDocument(QtGui.QTextDocument):
    """A rich text document that can resolve the board's picture URLs.

    Qt asks for every resource it cannot load itself, which is the hook that
    puts the pictures into the PDF without staging them as temporary files.
    """

    def __init__(self, attachments):
        super().__init__()
        self._attachments = attachments or {}
        self.pictures = 0

    def loadResource(self, resource_type, url):         # noqa: N802 - Qt
        if resource_type == QtGui.QTextDocument.ResourceType.ImageResource:
            image = self._picture(url.toString())
            if not image.isNull():
                self.pictures += 1
                return image
        return super().loadResource(resource_type, url)

    def _picture(self, address):
        from prism.fileio.attachments import attachment_id_of

        identifier = attachment_id_of(address)
        if identifier is None:
            return QtGui.QImage()
        entry = self._attachments.get(identifier)
        if not entry or len(entry) != 3:
            logger.info('PDF export is missing attachment %s', identifier)
            return QtGui.QImage()
        blob = entry[2]
        image = QtGui.QImage()
        if blob:
            image.loadFromData(blob)
        if image.isNull():
            logger.info('Attachment %s is not a picture Qt can draw',
                        identifier)
        return image


def export_note_to_pdf(html, attachments, target_path):
    """Write the note as a PDF, pictures included."""
    _check_writable(target_path)
    document = _AttachmentDocument(attachments)
    document.setDocumentMargin(0)
    document.setHtml(_pdf_html(html))

    writer = QtGui.QPdfWriter(str(target_path))
    writer.setResolution(PDF_RESOLUTION)
    writer.setPageSize(QtGui.QPageSize(QtGui.QPageSize.PageSizeId.A4))
    margin = QtCore.QMarginsF(PDF_MARGIN_MM, PDF_MARGIN_MM, PDF_MARGIN_MM,
                              PDF_MARGIN_MM)
    writer.setPageMargins(margin, QtGui.QPageLayout.Unit.Millimeter)
    try:
        # PyQt6 spells this one ``print``; it is a method on the document,
        # not the builtin.
        getattr(document, 'print')(writer)
    except Exception as exc:                            # noqa: BLE001
        raise DocumentExportError(
            _('Could not write {path}: {reason}')
            .format(path=target_path, reason=exc)) from exc

    if not os.path.exists(target_path) or os.path.getsize(target_path) == 0:
        raise DocumentExportError(
            _('Could not write {path}').format(path=target_path))
    if not document.pictures:
        logger.info('The PDF has no pictures, the note may not have any')
    return max(1, document.pageCount())


def _pdf_html(html):
    """The note as Qt should see it: pictures sized to fit the paper."""
    return html_with_fitted_images(html or '<p></p>', MAX_PAGE_IMAGE_WIDTH)


def _check_writable(target_path):
    """Fail early, and with a readable message, on a path nothing can open.

    Qt's PDF writer reports a path it cannot open by writing nothing at all,
    which is hard to tell apart from a damaged export.
    """
    directory = os.path.dirname(os.path.abspath(str(target_path)))
    if not os.path.isdir(directory):
        raise DocumentExportError(
            _('The folder {path} does not exist.').format(path=directory))
    try:
        with open(target_path, 'wb'):
            pass
    except OSError as exc:
        raise DocumentExportError(
            _('Could not write {path}: {reason}')
            .format(path=target_path, reason=exc)) from exc


# ── Plain text ────────────────────────────────────────────────


def export_note_to_text(html, attachments, target_path):
    """Write the note's text, without any formatting."""
    document = QtGui.QTextDocument()
    document.setHtml(html or '')
    try:
        with open(target_path, 'w', encoding='utf-8') as handle:
            handle.write(document.toPlainText())
    except OSError as exc:
        raise DocumentExportError(
            _('Could not write {path}: {reason}')
            .format(path=target_path, reason=exc)) from exc
    return 0


# ── Word ──────────────────────────────────────────────────────


def export_note_to_word(html, attachments, target_path):
    """Write the note as .docx, through pandoc when the user has it.

    pandoc keeps tables and styles closer to the original; python-docx is the
    fallback and is enough for the document features Prism offers.  Neither
    is bundled, so "no writer at all" is a real possibility and is reported
    rather than raised as a crash.
    """
    from prism.fileio.word import (WordExportError, export_note_to_docx,
                                   export_via_pandoc, pandoc_path,
                                   python_docx_available)

    writers = []
    if pandoc_path():
        writers.append(export_via_pandoc)
    if python_docx_available():
        writers.append(export_note_to_docx)
    if not writers:
        raise DocumentExportError(_(
            'Word export needs python-docx or pandoc.\n'
            'Install one of them first:\n'
            'pip install python-docx'))

    _check_writable(target_path)
    last_error = None
    for writer in writers:
        try:
            return writer(html, attachments, target_path)
        except WordExportError as exc:
            last_error = exc
            logger.info(f'{writer.__name__} could not export: {exc}')
        except Exception as exc:                        # noqa: BLE001
            last_error = exc
            logger.exception('Unexpected Word export failure')
    raise DocumentExportError(
        _('Word export failed: {reason}').format(reason=last_error))


# ── The list the file dialog shows ────────────────────────────

EXPORT_FORMATS = (
    ExportFormat('.docx', _('Word Document'), export_note_to_word,
                 _('Opens in Word and WPS')),
    ExportFormat('.pdf', _('PDF Document'), export_note_to_pdf,
                 _('Fixed layout, for printing and sharing')),
    ExportFormat('.html', _('Web Page'), export_note_to_html,
                 _('One file with the pictures inside')),
    ExportFormat('.txt', _('Plain Text'), export_note_to_text,
                 _('Text only, no pictures or formatting')),
)


def filter_string():
    """The file dialog filter listing every format that can be exported."""
    return ';;'.join(
        '{} (*{})'.format(entry.label, entry.extension)
        for entry in EXPORT_FORMATS)


def writer_for(target_path, chosen_filter=''):
    """Pick the writer for a path, taking the chosen filter into account.

    A user who typed ``note`` under the PDF filter means PDF, even though the
    path carries no extension yet.
    """
    path = str(target_path or '').lower()
    extension = os.path.splitext(path)[1]
    if extension:
        for entry in EXPORT_FORMATS:
            if entry.extension == extension:
                return entry
        # A known format under a different spelling (.jpeg style).
        if extension in ('.htm',):
            return _format_with('.html')
    chosen = str(chosen_filter or '').lower()
    for entry in EXPORT_FORMATS:
        if entry.label.lower() in chosen or entry.extension in chosen:
            return entry
    return EXPORT_FORMATS[0]


def _format_with(extension):
    for entry in EXPORT_FORMATS:
        if entry.extension == extension:
            return entry
    return EXPORT_FORMATS[0]


#: Other spellings that already mean a format.  ``note.htm`` is a web page,
#: and must not be saved as ``note.htm.html``.
EXTENSION_ALIASES = {
    '.html': ('.html', '.htm'),
    '.jpeg': ('.jpg', '.jpeg'),
}


def with_extension(target_path, entry):
    """A path that carries the extension the chosen format needs."""
    path = str(target_path)
    wanted = EXTENSION_ALIASES.get(entry.extension, (entry.extension,))
    return path if path.lower().endswith(wanted) else path + entry.extension
