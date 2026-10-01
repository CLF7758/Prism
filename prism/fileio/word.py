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

"""Word export for board notes.

The note is stored as the HTML the document editor produces (Quill), so the
export walks a QTextDocument built from that HTML instead of re-parsing it by
hand: that keeps headings, lists, tables and images exactly as they were
authored. ``normalize_editor_html`` bridges the two dialects first, because
Qt's rich text engine has no <pre> and would lose the code blocks.

pandoc is used when the user has it installed; otherwise python-docx writes
the file. Neither is bundled, and a missing one is reported rather than
raised.
"""

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile

from PyQt6 import QtCore, QtGui

from prism.i18n import _

logger = logging.getLogger(__name__)


#: Widest an embedded image may be, in centimetres.
MAX_IMAGE_WIDTH_CM = 14.0

#: Formats python-docx embeds without any help from Qt.  A picture stored in
#: one of these is handed over exactly as it was imported, because putting it
#: through QImage would re-encode it and drop its metadata (EXIF, ICC, the
#: original JPEG tables) for good.
EMBEDDABLE_FORMATS = ('png', 'jpg', 'jpeg', 'gif', 'bmp', 'tif', 'tiff')

#: Header bytes of those formats.  python-docx sniffs the stream the same way
#: and refuses anything it does not recognise, so the check has to happen
#: before the bytes are handed over.
_IMAGE_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'\xff\xd8\xff', 'jpg'),
    (b'GIF87a', 'gif'),
    (b'GIF89a', 'gif'),
    (b'BM', 'bmp'),
    (b'II*\x00', 'tiff'),
    (b'MM\x00*', 'tiff'),
)

#: Families that mean "this is a code block" even when Qt does not report
#: fontFixedPitch for the CSS font stack the editor writes.
MONOSPACE_FAMILIES = ('monospace', 'consolas', 'courier', 'courier new',
                      'menlo', 'monaco', 'cascadia', 'dejavu sans mono',
                      'sf mono', 'source code pro', 'fira code')

_PRE_BLOCK = re.compile(r'<pre\b[^>]*>(.*?)</pre>', re.IGNORECASE | re.DOTALL)


def normalize_editor_html(html):
    """Translate editor HTML into the subset QTextDocument understands.

    Quill writes a code block as ``<pre data-language="...">``. Qt has no
    <pre> in its supported HTML and flattens the element into an ordinary
    paragraph, which loses the monospaced text and the block indent that the
    Word writer keys on to recognise a code block.
    """
    if not html:
        return ''
    return _PRE_BLOCK.sub(_pre_to_paragraph, html)


def _pre_to_paragraph(match):
    body = match.group(1).strip('\n')
    body = body.replace('\r\n', '\n').replace('\n', '<br />')
    return ('<p style="-qt-block-indent:1; '
            "font-family:Consolas,'Courier New',monospace\">"
            f'{body}</p>')


class WordExportError(Exception):
    """Raised when the note cannot be turned into a Word document."""


def python_docx_available():
    try:
        import docx                                # noqa: F401
    except ImportError:
        return False
    return True


def pandoc_path():
    """Path to pandoc, or None when it is not installed."""
    return shutil.which('pandoc')


def export_note_to_docx(html, attachments, target_path, template_path=None):
    """Write ``html`` to ``target_path`` as .docx.

    :param attachments: ``{id: (name, mime, blob)}`` for prism:// images
    :param template_path: optional .docx whose styles are reused
    :returns: the number of images embedded
    :raises WordExportError: when no writer is available or writing fails
    """
    if not python_docx_available():
        raise WordExportError(
            _('Word export needs python-docx. Install it with:\n'
              'pip install python-docx'))

    document = QtGui.QTextDocument()
    document.setHtml(normalize_editor_html(html))
    used_images = _images_in(document, attachments)

    try:
        import docx
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError as exc:                     # pragma: no cover
        raise WordExportError(str(exc)) from exc

    if template_path and os.path.exists(template_path):
        word = docx.Document(template_path)
    else:
        word = docx.Document()

    _apply_base_style(word)

    block = document.begin()
    while block.isValid():
        _write_block(word, block, used_images, Cm, Pt, WD_ALIGN_PARAGRAPH)
        block = block.next()

    try:
        word.save(target_path)
    except OSError as exc:
        raise WordExportError(
            _('Could not write {path}: {reason}')
            .format(path=target_path, reason=exc)) from exc
    return len(used_images)


def export_via_pandoc(html, attachments, target_path, template_path=None):
    """Export through pandoc, which keeps tables and styles faithful.

    Only used when the user has pandoc installed; Prism never bundles it.
    """
    executable = pandoc_path()
    if not executable:
        raise WordExportError(_('pandoc was not found on PATH'))

    workdir = tempfile.mkdtemp(prefix='prism-word-')
    try:
        markdown = html_to_markdown(html, attachments, workdir)
        source = os.path.join(workdir, 'note.md')
        with io.open(source, 'w', encoding='utf-8') as handle:
            handle.write(markdown)

        command = [executable, source, '--resource-path', workdir,
                   '-o', target_path]
        if template_path and os.path.exists(template_path):
            command += ['--reference-doc', template_path]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise WordExportError(
                _('pandoc failed: {reason}')
                .format(reason=(result.stderr or '').strip() or
                        f'exit code {result.returncode}'))
        return len(attachments)
    except subprocess.TimeoutExpired as exc:
        raise WordExportError(_('pandoc timed out')) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def html_to_markdown(html, attachments, image_dir=None):
    """Convert the note to Markdown, writing images next to it.

    Used by the pandoc path and available on its own for ``.md`` export.
    """
    document = QtGui.QTextDocument()
    document.setHtml(normalize_editor_html(html))
    used_images = _images_in(document, attachments)

    lines = []
    block = document.begin()
    while block.isValid():
        kind, payload = _describe_block(block)
        if kind == 'heading':
            text, level = payload
            lines.append('#' * max(1, min(level, 6)) + ' ' + text)
        elif kind == 'list':
            text, style = payload
            marker = '1. ' if style == 'decimal' else '- '
            lines.append('  ' * block.blockFormat().indent() + marker + text)
        elif kind == 'code':
            lines.append('```')
            lines.append(payload)
            lines.append('```')
        elif kind == 'image':
            name = _write_image(payload, used_images, image_dir)
            if name:
                lines.append(f'![{name}]({name})')
        elif kind == 'table':
            lines.append(payload)
        else:
            lines.append(payload)
        if lines and lines[-1] != '':
            lines.append('')
        block = block.next()
    return '\n'.join(lines).rstrip() + '\n'


# ── Block inspection ─────────────────────────────────────────────


def _describe_block(block):
    """Classify a text block into (kind, payload)."""
    image = _block_image(block)
    if image is not None:
        return 'image', image

    text = block.text().strip()
    block_format = block.blockFormat()
    heading = block_format.headingLevel()
    if heading:
        return 'heading', (text, heading)

    text_list = block.textList()
    if text_list is not None:
        style = text_list.format().style()
        name = 'decimal' if style == QtGui.QTextListFormat.Style.ListDecimal \
            else 'bullet'
        return 'list', (text, name)

    if block_format.indent() and _looks_monospaced(block):
        return 'code', block.text()

    return 'paragraph', text


def _looks_monospaced(block):
    """Whether the block is set in a fixed-pitch face.

    Qt reports ``fontFixedPitch`` for the fonts it resolves itself, but a CSS
    font stack like ``Consolas, 'Courier New', monospace`` does not always
    come back that way, so the well-known names are accepted as well.
    """
    iterator = block.begin()
    seen = False
    while not iterator.atEnd():
        fragment = iterator.fragment()
        if fragment.isValid():
            seen = True
            char_format = fragment.charFormat()
            if not (char_format.fontFixedPitch()
                    or _is_monospace_family(char_format)):
                return False
        iterator += 1
    return seen


def _is_monospace_family(char_format):
    try:
        families = list(char_format.fontFamilies() or [])
    except AttributeError:                          # pragma: no cover - Qt 5
        families = []
    if not families:
        name = char_format.fontFamily()
        families = [name] if name else []
    for family in families:
        name = str(family).strip().strip('\'"').lower()
        if name in MONOSPACE_FAMILIES or 'mono' in name or 'courier' in name:
            return True
    return False


def _block_image(block):
    """Return the image format of a block that holds one, else None."""
    iterator = block.begin()
    while not iterator.atEnd():
        fragment = iterator.fragment()
        if fragment.isValid():
            char_format = fragment.charFormat()
            if char_format.isImageFormat():
                return char_format.toImageFormat()
        iterator += 1
    return None


def _images_in(document, attachments):
    """Every attachment the note actually references, by URL."""
    used = {}
    block = document.begin()
    while block.isValid():
        image_format = _block_image(block)
        if image_format is not None:
            name = image_format.name()
            attachment_id = _attachment_id(name)
            if attachment_id is not None and attachment_id in attachments:
                used[name] = attachments[attachment_id]
        block = block.next()
    return used


def _attachment_id(url):
    """Extract the id from ``prism://attach/<id>``."""
    prefix = 'prism://attach/'
    if not url.startswith(prefix):
        return None
    try:
        return int(url[len(prefix):])
    except ValueError:
        return None


# ── Writers ──────────────────────────────────────────────────────


def _apply_base_style(word):
    """Give the document a predictable, printable default look."""
    try:
        from docx.shared import Pt
        style = word.styles['Normal']
        style.font.name = 'Microsoft YaHei'
        style.font.size = Pt(11)
    except KeyError:                                # pragma: no cover
        logger.debug('Normal style missing from the template')


def _write_block(word, block, used_images, Cm, Pt, alignment):
    """Append one note block to the Word document."""
    kind, payload = _describe_block(block)
    if kind == 'heading':
        text, level = payload
        if text:
            word.add_heading(text, level=level)
    elif kind == 'list':
        text, style = payload
        if not text:
            return
        if style == 'decimal':
            word.add_paragraph(text, style='List Number')
        else:
            word.add_paragraph(text, style='List Bullet')
    elif kind == 'code':
        paragraph = word.add_paragraph(payload)
        run = paragraph.runs[0] if paragraph.runs else paragraph.add_run('')
        run.font.name = 'Consolas'
        run.font.size = Pt(10)
    elif kind == 'image':
        image_format = payload
        entry = used_images.get(image_format.name())
        if entry is None:
            logger.debug('Skipping image with no attachment: %s',
                         image_format.name())
            return
        _add_image(word, entry, Cm, alignment)
    else:
        if payload:
            word.add_paragraph(payload)


def _add_image(word, entry, Cm, alignment):
    name, _mime, blob = entry
    stream = _embedded_image(blob, name)
    if stream is None:
        logger.info('Skipping attachment %s', name)
        return

    paragraph = word.add_paragraph()
    paragraph.alignment = alignment.CENTER
    run = paragraph.add_run()
    try:
        run.add_picture(stream, width=Cm(MAX_IMAGE_WIDTH_CM))
    except Exception:                               # pragma: no cover
        logger.exception('Could not embed image %s', name)
        return
    if name:
        caption = word.add_paragraph(name)
        caption.alignment = alignment.CENTER


def image_format(blob):
    """The format of an image blob, read from its header, or ``None``."""
    if not blob:
        return None
    for signature, name in _IMAGE_SIGNATURES:
        if blob.startswith(signature):
            return name
    return None


def _embedded_image(blob, name):
    """A stream python-docx can embed, or ``None`` when the blob is unusable.

    Bytes already in a format Word takes are handed over *untouched*: running
    a JPEG through QImage would re-encode it, and the quality that was lost
    that way cannot be recovered.  Anything else is decoded and written as
    PNG, and a blob that is not a picture at all is skipped rather than
    stored as something Word refuses to open.
    """
    if image_format(blob) in EMBEDDABLE_FORMATS:
        return io.BytesIO(blob)
    image = QtGui.QImage.fromData(blob)
    if image.isNull():
        logger.info('Attachment %s is not a readable image', name)
        return None
    data = _encode_with_qt(image, name)
    return io.BytesIO(data) if data else None


def _encode_with_qt(image, name):
    """Encode an image Qt had to convert, as lossless PNG."""
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        buffer.close()
        logger.info('Could not encode image %s as PNG', name)
        return None
    data = bytes(buffer.data())
    buffer.close()
    return data


def _write_image(image_format, used_images, image_dir):
    """Write one image to disk for the pandoc path, returning its name."""
    if not image_dir:
        return None
    entry = used_images.get(image_format.name())
    if entry is None:
        return None
    name, _mime, blob = entry
    safe = os.path.basename(name) or 'image.png'
    target = os.path.join(image_dir, safe)
    try:
        with open(target, 'wb') as handle:
            handle.write(blob)
    except OSError:
        logger.exception('Could not write image %s', target)
        return None
    return safe
