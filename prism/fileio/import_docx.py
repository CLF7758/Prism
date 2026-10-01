"""Read .docx files into HTML for the note editor.

`.docx` is a ZIP archive holding WordprocessingML.  `python-docx` does the
parsing; this module walks the body in document order so paragraphs and
tables keep their relative position.  No code or asset from Microsoft
Word is used; the Word to HTML mapping tables live in
:mod:`prism.fileio.docx_styles`.

What comes across:

* headings, found through the style name *and* through `w:outlineLvl`, so
  a Word that writes "标题 1" is read the same as one writing "heading 1"
* character formatting: font family, size, colour, highlight, bold,
  italic, underline, strike-through, superscript and subscript
* paragraph formatting: alignment, indent, line spacing and the space
  before/after a paragraph
* lists, including nesting, resolved through `numbering.xml` - Word keeps
  list items as flat paragraphs that each carry their level
* tables (including merged cells), inline images, hyperlinks, tabs and
  line breaks
* empty paragraphs, which Word uses to space a document out

Headers and footers, sections, columns, text boxes, footnotes, comments,
equations and tracked changes are **left out rather than guessed at**.  The
task book is explicit that this has to be said rather than done quietly, so
:class:`ImportReport` counts what was skipped and :func:`docx_to_html` fills
one in for every import.
"""
import collections
import html
import io
import logging
import os

from .import_base import ImportFileError
from .docx_styles import (ALIGNMENTS, css_color, css_length, css_point_size,
                          emu_to_px, font_stack, heading_level,
                          highlight_color, list_tag, ordered_type)
from prism.i18n import _

try:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
except ImportError:                                     # pragma: no cover
    docx = None
    Table = None
    Paragraph = None

logger = logging.getLogger(__name__)


#: XML namespaces this reader looks inside.
_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_WP = ('{http://schemas.openxmlformats.org/drawingml/2006/'
       'wordprocessingDrawing}')
_M = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'

#: Run content Word stores that HTML spells as an entity.
_RUN_MARKS = {
    'tab': '&#9;',
    'noBreakHyphen': '-',
    'softHyphen': '&shy;',
}

#: Word features this reader does not carry over, and the tag that says a
#: document uses one.  A feature that is present is reported, so the user
#: learns what did not come across instead of finding out later.
_SKIPPED_FEATURES = (
    ('header', _W + 'headerReference'),
    ('header', _W + 'footerReference'),
    ('footnote', _W + 'footnoteReference'),
    ('footnote', _W + 'endnoteReference'),
    ('comment', _W + 'commentReference'),
    ('revision', _W + 'ins'),
    ('revision', _W + 'del'),
    ('formula', _M + 'oMath'),
    ('textbox', _W + 'txbxContent'),
)

#: Feature key -> the words the user is shown.
SKIPPED_FEATURES = {
    'header': _('headers and footers'),
    'footnote': _('footnotes and endnotes'),
    'comment': _('comments'),
    'revision': _('tracked changes'),
    'formula': _('equations'),
    'textbox': _('text boxes'),
}


class ImportReport:
    """What a Word file held that Prism could not bring across.

    Handed to :func:`docx_to_html` so the caller can tell the user.  An
    import that drops a document's headers without saying so is worse than
    one that refuses it: the user believes the whole file came through.
    """

    def __init__(self):
        #: Pictures handed to the attachment saver.
        self.images = 0
        #: Pictures that had to be scaled down to be displayable at all.
        self.images_scaled = 0
        #: Feature key -> how many places the document used it.
        self.skipped = collections.Counter()

    def note_skipped(self, feature, count=1):
        self.skipped[feature] += count

    def summary(self):
        """One sentence for the user, or ``None`` when nothing was left out."""
        if not self.skipped:
            return None
        names = '、'.join(SKIPPED_FEATURES.get(feature, feature)
                         for feature in sorted(self.skipped))
        message = _('This Word file also contains {features}. '
                    'Prism leaves those out when importing.')
        if self.images_scaled:
            message += ' ' + _(
                '{count} picture(s) were too large for the editor and were '
                'scaled down.').format(count=self.images_scaled)
        return message.format(features=names)

    def as_dict(self):
        """The same facts as plain data, for logs and tests."""
        return {'images': self.images,
                'images_scaled': self.images_scaled,
                'skipped': dict(self.skipped)}


def scan_skipped_features(element, report):
    """Record the features of one document body this reader does not carry."""
    for feature, tag in _SKIPPED_FEATURES:
        if next(element.iter(tag), None) is not None:
            report.note_skipped(feature)

#: Largest number of pixels an imported picture may have.  Qt refuses to
#: decode anything past its 256 MB allocation limit, so a bigger picture
#: would be stored in the board without ever being shown.  40 MP is a
#: 160 MB RGBA buffer, which leaves the editor room to draw it.
MAX_IMAGE_PIXELS = 40_000_000


def _pixel_size(data):
    """Return ``(width, height)`` of an image without decoding it."""
    try:
        from PIL import Image
    except ImportError:                                 # pragma: no cover
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Image.DecompressionBombError:
        # Pillow's own guard refused to look at it, but the header is all
        # this needs.
        return _png_size(data)
    except Exception:                                   # noqa: BLE001
        return None


def _png_size(data):
    """Read width and height straight out of a PNG header."""
    if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' \
            or data[12:16] != b'IHDR':
        return None
    return (int.from_bytes(data[16:20], 'big'),
            int.from_bytes(data[20:24], 'big'))


def _scale_image(data, target, extension):
    """Return ``(bytes, extension)`` for a scaled copy of an image."""
    try:
        from PIL import Image
    except ImportError:                                 # pragma: no cover
        logger.info('Pillow is unavailable, storing images at full size')
        return data, extension
    previous = Image.MAX_IMAGE_PIXELS
    try:
        # Looking at a picture the size of a wall poster is exactly what
        # this code is for, so Pillow's bomb guard is lifted for the one
        # image being scaled.
        Image.MAX_IMAGE_PIXELS = None
        image = Image.open(io.BytesIO(data))
        resized = image.convert('RGBA').resize(target, Image.LANCZOS)
    except Exception:                                   # noqa: BLE001
        logger.exception('Could not scale an embedded image down')
        return data, extension
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    buffer = io.BytesIO()
    if extension in ('jpg', 'jpeg'):
        resized.convert('RGB').save(buffer, 'JPEG', quality=90)
        return buffer.getvalue(), 'jpg'
    resized.save(buffer, 'PNG', optimize=True)
    return buffer.getvalue(), 'png'


def docx_to_html(path, attachment_saver=None, max_image_edge=None,
                 report=None):
    """Convert a .docx document into HTML for the note editor.

    ``attachment_saver`` receives ``(bytes, extension)`` for every embedded
    image and must return a URL to reference it by.  The bytes are handed
    over exactly as the file stores them, so an imported picture keeps its
    original quality.  Pass ``max_image_edge`` to have anything larger than
    that many pixels scaled down first - that has to re-encode the image,
    which is why it is off by default.  A picture with more pixels than the
    editor can decode is scaled down either way, because storing it would
    only spend room on something that can never be shown.

    ``report`` is an :class:`ImportReport` the caller can read afterwards to
    find out what the document held that Prism does not carry across.
    """
    if docx is None:
        raise ImportFileError(_(
            'python-docx is missing, so Word documents cannot be read.'))
    try:
        document = docx.Document(path)
    except Exception as error:              # python-docx raises many types
        raise ImportFileError(_('Could not read the .docx file: {error}')
                              .format(error=error)) from error

    if report is None:
        report = ImportReport()
    scan_skipped_features(document.element.body, report)

    importer = _DocumentImporter(document, attachment_saver, max_image_edge,
                                 report)
    markup = importer.run()
    report.images = importer.image_count
    logger.info('Imported %s: %d images, %d characters of HTML',
                os.path.basename(str(path)), importer.image_count,
                len(markup))
    return markup


def _local_name(tag):
    """Return an XML tag without its namespace."""
    return str(tag).rsplit('}', 1)[-1]


def _is_on(value):
    """Read Word's tri-state boolean (``True``, ``False`` or ``None``)."""
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return value
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value)


def _css_text(properties):
    """Render a property dict as a CSS attribute value."""
    return ';'.join(f'{name}:{value}' for name, value in properties.items()
                    if value)


def _font_names(element):
    """Return the fonts a style or run asks for, the Latin one first."""
    from docx.oxml.ns import qn

    if element is None:
        return []
    properties = element.find(qn('w:rPr'))
    if properties is None:
        return []
    fonts = properties.find(qn('w:rFonts'))
    if fonts is None:
        return []
    return [fonts.get(qn('w:ascii')), fonts.get(qn('w:eastAsia')),
            fonts.get(qn('w:hAnsi'))]


def _image_size(drawing):
    """Return the size Word displays an image at, in CSS pixels."""
    extent = drawing.find(f'.//{_WP}extent')
    if extent is None:
        return None
    width = emu_to_px(extent.get('cx'))
    height = emu_to_px(extent.get('cy'))
    if not width or not height:
        return None
    return int(round(width)), int(round(height))


def _relations_of(run):
    """Return the image relationship ids a run refers to."""
    found = set()
    for blip in run._element.findall(f'.//{_A}blip'):
        relation_id = blip.get(f'{_R}embed') or blip.get(f'{_R}link')
        if relation_id:
            found.add(relation_id)
    return found


def _iter_blocks(body):
    """Yield paragraphs and tables in the order they appear.

    Content inside a structured document tag (``w:sdt``, what Word puts
    around a table of contents or a content control) is unwrapped instead
    of skipped, so those chapters still arrive.
    """
    for child in body.iterchildren():
        tag = _local_name(child.tag)
        if tag == 'p':
            yield 'paragraph', child
        elif tag == 'tbl':
            yield 'table', child
        elif tag == 'sdt':
            content = child.find(f'{_W}sdtContent')
            if content is not None:
                yield from _iter_blocks(content)


class _Numbering:
    """Resolves the numbering format a numbered paragraph uses."""

    def __init__(self, document):
        self._document = document
        self._abstract = {}
        self._numbers = {}
        self._ready = False

    def _load(self):
        self._ready = True
        try:
            element = self._document.part.numbering_part.element
        except (AttributeError, KeyError, NotImplementedError,
                ValueError) as error:
            logger.debug('Document has no usable numbering part: %s', error)
            return
        for node in element.findall(f'{_W}abstractNum'):
            levels = {}
            for level in node.findall(f'{_W}lvl'):
                fmt = level.find(f'{_W}numFmt')
                levels[level.get(f'{_W}ilvl')] = (
                    fmt.get(f'{_W}val') if fmt is not None else None)
            self._abstract[node.get(f'{_W}abstractNumId')] = levels
        for node in element.findall(f'{_W}num'):
            reference = node.find(f'{_W}abstractNumId')
            self._numbers[node.get(f'{_W}numId')] = (
                reference.get(f'{_W}val') if reference is not None else None)

    def format_for(self, num_id, level):
        """Return the ``w:numFmt`` of one list level, or ``None``."""
        if not self._ready:
            self._load()
        abstract_id = self._numbers.get(str(num_id))
        if abstract_id is None:
            return None
        return (self._abstract.get(abstract_id) or {}).get(str(level))


class _ListWriter:
    """Turns a flat run of numbered paragraphs back into nested lists.

    Word stores list items as ordinary paragraphs that each carry their
    level, so the nesting has to be rebuilt while walking the body.  An
    open list stays open while the numbering continues: bullets carry on
    across a change of ``numId`` (Word hands out a fresh id for almost
    every item), numbered lists restart so their counters stay right.
    """

    def __init__(self):
        self._open = []

    def item(self, level, kind, ordered, num_id, content):
        """Append one list item and return the markup around it."""
        level = max(0, min(level, len(self._open)))
        chunks = []
        while self._open and self._open[-1]['level'] > level:
            chunks.append(self._close_one())
        if self._open and self._open[-1]['level'] == level:
            if self._continues(kind, ordered, num_id):
                chunks.append('</li>')
            else:
                chunks.append(self._close_one())
        while len(self._open) < level + 1:
            chunks.append(self._open_list(kind, ordered, num_id))
        chunks.append(f'<li>{content}')
        return ''.join(chunks)

    def close(self):
        """Close every open list and return the markup for it."""
        chunks = []
        while self._open:
            chunks.append(self._close_one())
        return ''.join(chunks)

    def _open_list(self, kind, ordered, num_id):
        attributes = f' type="{ordered}"' if kind == 'ol' and ordered else ''
        self._open.append({'level': len(self._open), 'kind': kind,
                           'ordered': ordered, 'num_id': num_id})
        return f'<{kind}{attributes}>'

    def _continues(self, kind, ordered, num_id):
        top = self._open[-1]
        if top['kind'] != kind or top['ordered'] != ordered:
            return False
        if kind == 'ul':
            return True
        return top['num_id'] == num_id

    def _close_one(self):
        entry = self._open.pop()
        return f'</li></{entry["kind"]}>'


class _DocumentImporter:
    """Walks one .docx document and builds its HTML."""

    def __init__(self, document, attachment_saver=None, max_image_edge=None,
                 report=None):
        self.document = document
        self.saver = attachment_saver
        self.max_image_edge = max_image_edge
        self.report = report if report is not None else ImportReport()
        self.numbering = _Numbering(document)
        self.image_count = 0
        self._stored_images = {}
        self._style_cache = {}

    # ── Blocks ────────────────────────────────────────────────────

    def run(self):
        chunks = []
        lists = _ListWriter()
        for kind, element in _iter_blocks(self.document.element.body):
            if kind == 'table':
                chunks.append(lists.close())
                chunks.append(self._table_html(Table(element, self.document)))
                continue
            paragraph = Paragraph(element, self.document)
            info = self._numbering_of(paragraph)
            if info is None:
                chunks.append(lists.close())
                chunks.append(self._paragraph_html(paragraph))
            else:
                level, list_kind, ordered, num_id = info
                content = self._paragraph_html(paragraph, in_list=True)
                chunks.append(lists.item(level, list_kind, ordered, num_id,
                                         content))
        chunks.append(lists.close())
        markup = ''.join(chunk for chunk in chunks if chunk)
        return markup if markup.strip() else '<p><br /></p>'

    def _numbering_of(self, paragraph):
        """Return ``(level, tag, ordered type, numId)`` or ``None``."""
        properties = self._number_properties(paragraph)
        if properties is None:
            return None
        node = properties.find(f'{_W}numId')
        num_id = node.get(f'{_W}val') if node is not None else None
        if num_id in (None, '0'):
            return None
        node = properties.find(f'{_W}ilvl')
        raw_level = node.get(f'{_W}val') if node is not None else '0'
        try:
            level = int(raw_level)
        except (TypeError, ValueError):
            level = 0
        number_format = self.numbering.format_for(num_id, level)
        kind = list_tag(number_format)
        if kind is None:
            return None
        return max(0, level), kind, ordered_type(number_format), num_id

    @staticmethod
    def _number_properties(paragraph):
        """Find ``w:numPr`` on the paragraph or on the style it uses."""
        node = paragraph._p.find(f'{_W}pPr')
        if node is not None:
            properties = node.find(f'{_W}numPr')
            if properties is not None:
                return properties
        # "List Bullet" and friends keep the numbering in the style, and
        # their names differ per language - so the style has to be read.
        style = getattr(paragraph, 'style', None)
        seen = set()
        while style is not None and id(style) not in seen:
            seen.add(id(style))
            element = getattr(style, 'element', None)
            node = element.find(f'{_W}pPr') if element is not None else None
            if node is not None:
                properties = node.find(f'{_W}numPr')
                if properties is not None:
                    return properties
            style = getattr(style, 'base_style', None)
        return None

    # ── Paragraphs ────────────────────────────────────────────────

    def _paragraph_html(self, paragraph, in_list=False):
        base = self._cached_style_properties(getattr(paragraph, 'style', None))
        content, has_text, has_image = self._paragraph_content(paragraph, base)
        level = heading_level(getattr(paragraph, 'style', None))
        if in_list:
            # A list item's position comes from the list nesting, so the
            # paragraph's own indent and spacing are left out.  A heading
            # inside a list still has to carry its style's font, though.
            _align, properties = self._paragraph_properties(
                paragraph, base, layout=False)
            attributes = ''
            style_attribute = _css_text(properties)
            if style_attribute:
                attributes = f' style="{style_attribute}"'
            if level and content:
                return f'<h{level}{attributes}>{content}</h{level}>'
            return content or '<br />'

        align, properties = self._paragraph_properties(paragraph, base)
        if not has_text and not has_image:
            # Word spaces a document out with empty paragraphs; keeping
            # them keeps the rhythm of the original.
            content = '<br />'
        tag = f'h{level}' if level else 'p'
        attributes = f' align="{align}"' if align else ''
        style_attribute = _css_text(properties)
        if style_attribute:
            attributes += f' style="{style_attribute}"'
        return f'<{tag}{attributes}>{content}</{tag}>'

    def _paragraph_properties(self, paragraph, base, layout=True):
        """Return ``(alignment, CSS properties)`` for one paragraph.

        With ``layout=False`` only the font is resolved, which is what a
        paragraph needs when it lives inside a list item.
        """
        properties = dict(base)
        if not layout:
            return None, properties
        alignment = paragraph.alignment
        align = None
        if alignment is not None:
            try:
                align = ALIGNMENTS.get(int(alignment))
            except (TypeError, ValueError):
                align = None
            if align:
                # Both spellings: HTML 4's attribute is what Qt honours,
                # the CSS property is what a browser honours.
                properties['text-align'] = align

        format_ = paragraph.paragraph_format
        left = emu_to_px(format_.left_indent)
        if left:
            properties['margin-left'] = css_length(left)
        first_line = emu_to_px(format_.first_line_indent)
        if first_line:
            properties['text-indent'] = css_length(first_line)
        spacing = format_.line_spacing
        if spacing is not None:
            if isinstance(spacing, float):
                properties['line-height'] = f'{round(float(spacing), 2)}'
            else:
                properties['line-height'] = css_point_size(spacing.pt)
        before = emu_to_px(format_.space_before)
        if before is not None:
            properties['margin-top'] = css_length(before)
        after = emu_to_px(format_.space_after)
        if after is not None:
            properties['margin-bottom'] = css_length(after)
        return align, properties

    def _paragraph_content(self, paragraph, base):
        """Return ``(markup, has text, has image)`` for a paragraph."""
        pieces = []
        has_text = False
        has_image = False
        used = set()
        for item in self._inline_items(paragraph):
            runs = getattr(item, 'runs', None)
            is_link = runs is not None
            if runs is None:
                runs = (item,)
            inner = []
            for run in runs:
                # Remembered so the sweep below does not store a picture
                # a second time.
                used.update(_relations_of(run))
                markup, text, image = self._run_html(run, base)
                inner.append(markup)
                has_text = has_text or text
                has_image = has_image or image
            joined = ''.join(inner)
            if not joined:
                continue
            if is_link:
                address = getattr(item, 'address', '') or ''
                joined = (f'<a href="{html.escape(address, quote=True)}">'
                          f'{joined}</a>')
            pieces.append(joined)

        # Safety net for pictures that hang off something other than a
        # plain run, so no image is ever silently dropped.
        for blip in paragraph._p.findall(f'.//{_A}blip'):
            relation_id = blip.get(f'{_R}embed')
            if not relation_id or relation_id in used:
                continue
            used.add(relation_id)
            markup = self._image_html(paragraph.part, relation_id)
            if markup:
                pieces.append(markup)
                has_image = True
        return ''.join(pieces), has_text, has_image

    @staticmethod
    def _inline_items(paragraph):
        """Yield the runs and hyperlinks of a paragraph, in order."""
        try:
            yield from paragraph.iter_inner_content()
        except Exception:                               # noqa: BLE001
            logger.debug('Falling back to plain runs for one paragraph')
            yield from paragraph.runs

    # ── Runs ──────────────────────────────────────────────────────

    def _run_html(self, run, base):
        """Return ``(markup, has text, has image)`` for one run."""
        font = run.font
        if _is_on(getattr(font, 'hidden', None)):
            return '', False, False
        pieces = []
        has_text = False
        has_image = False
        for node in run._element.iterchildren():
            tag = _local_name(node.tag)
            if tag == 't':
                text = node.text or ''
                if text:
                    pieces.append(html.escape(text))
                    has_text = True
            elif tag in _RUN_MARKS:
                pieces.append(_RUN_MARKS[tag])
                has_text = True
            elif tag in ('br', 'cr'):
                pieces.append('<br />')
                has_text = True
            elif tag == 'drawing':
                markup = self._drawing_html(run, node)
                if markup:
                    pieces.append(markup)
                    has_image = True
        content = ''.join(pieces)
        if not content:
            return '', False, False

        properties = self._run_properties(run, base)
        difference = {name: value for name, value in properties.items()
                      if base.get(name) != value}

        base_weight = base.get('font-weight')
        bold = font.bold
        bold = (base_weight == '700') if bold is None else _is_on(bold)
        base_italic = base.get('font-style') == 'italic'
        italic = font.italic
        italic = base_italic if italic is None else _is_on(italic)
        underlined = _is_on(run.underline)
        struck = _is_on(font.strike) or _is_on(font.double_strike)
        base_decoration = base.get('text-decoration') or ''
        base_underline = 'underline' in base_decoration
        base_strike = 'line-through' in base_decoration

        # Word's formatting is a diff on top of the paragraph style, so a
        # property only has to survive here when it changes.  Semantic tags
        # are written as well, because a browser engine (Quill, the editor
        # the document panel is moving to) reads <b> rather than a style.
        if bold and base_weight != '700':
            content = f'<b>{content}</b>'
            difference.pop('font-weight', None)
        if italic and not base_italic:
            content = f'<i>{content}</i>'
            difference.pop('font-style', None)
        if underlined and not base_underline:
            content = f'<u>{content}</u>'
        if struck and not base_strike:
            content = f'<s>{content}</s>'
        if difference.get('text-decoration') and not base_decoration:
            difference.pop('text-decoration', None)
        if difference:
            content = f'<span style="{_css_text(difference)}">{content}</span>'
        if _is_on(font.superscript):
            content = f'<sup>{content}</sup>'
        elif _is_on(font.subscript):
            content = f'<sub>{content}</sub>'
        return content, has_text, has_image

    def _run_properties(self, run, base):
        """Resolve a run's formatting, falling back to the paragraph."""
        properties = dict(base)
        font = run.font
        family = font_stack(_font_names(run._element))
        if family:
            properties['font-family'] = family
        size = font.size
        if size is not None:
            properties['font-size'] = css_point_size(size.pt)
        if font.bold is not None:
            properties['font-weight'] = '700' if font.bold else '400'
        if font.italic is not None:
            properties['font-style'] = 'italic' if font.italic else 'normal'
        if run.underline is not None or font.strike is not None \
                or font.double_strike is not None:
            decorations = []
            if _is_on(run.underline):
                decorations.append('underline')
            if _is_on(font.strike) or _is_on(font.double_strike):
                decorations.append('line-through')
            properties['text-decoration'] = ' '.join(decorations) or 'none'
        try:
            color = css_color(font.color.rgb)
        except (AttributeError, ValueError):
            color = None
        if color:
            properties['color'] = color
        background = highlight_color(font.highlight_color)
        if background:
            properties['background-color'] = background
        return properties

    @staticmethod
    def _style_properties(style):
        """Resolve a paragraph or character style into CSS properties."""
        properties = {}
        font = getattr(style, 'font', None)
        if font is None:
            return properties
        family = font_stack(_font_names(getattr(style, 'element', None)))
        if family:
            properties['font-family'] = family
        size = font.size
        if size is not None:
            properties['font-size'] = css_point_size(size.pt)
        bold = font.bold
        if bold is not None:
            properties['font-weight'] = '700' if bold else '400'
        italic = font.italic
        if italic is not None:
            properties['font-style'] = 'italic' if italic else 'normal'
        try:
            color = css_color(font.color.rgb)
        except (AttributeError, ValueError):
            color = None
        if color:
            properties['color'] = color
        return properties

    def _cached_style_properties(self, style):
        """Same as `_style_properties`, but resolved once per style."""
        key = (getattr(style, 'style_id', None)
               or getattr(style, 'name', None) or '')
        if key not in self._style_cache:
            self._style_cache[key] = self._style_properties(style)
        return self._style_cache[key]

    # ── Images ────────────────────────────────────────────────────

    def _drawing_html(self, run, drawing):
        blip = drawing.find(f'.//{_A}blip')
        if blip is None:
            return ''
        relation_id = blip.get(f'{_R}embed') or blip.get(f'{_R}link')
        if not relation_id:
            return ''
        return self._image_html(run.part, relation_id, _image_size(drawing))

    def _image_html(self, part, relation_id, size=None):
        url = self._store_image(part, relation_id)
        if not url:
            return ''
        attributes = f' src="{html.escape(url, quote=True)}"'
        if size is not None:
            attributes += f' width="{size[0]}" height="{size[1]}"'
        return f'<img{attributes} />'

    def _store_image(self, part, relation_id):
        """Hand one embedded image to the attachment saver, once.

        Word reuses one media part for a picture that appears twice, so the
        result is remembered per part and relationship id.
        """
        if self.saver is None:
            return ''
        key = (str(getattr(part, 'partname', '')), relation_id)
        if key in self._stored_images:
            return self._stored_images[key]
        url = ''
        related = getattr(part, 'related_parts', {}).get(relation_id)
        data = getattr(related, 'blob', None) if related is not None else None
        if data:
            extension = (str(getattr(related, 'partname', '') or '')
                         .rsplit('.', 1)[-1].lower() or 'png')
            data, extension = self._prepare_image(data, extension)
            try:
                url = self.saver(data, extension) or ''
            except Exception:                           # noqa: BLE001
                logger.exception('Could not store an embedded image')
                url = ''
            if url:
                self.image_count += 1
        else:
            logger.debug('Image %s is missing from the package', relation_id)
        self._stored_images[key] = url
        return url

    def _prepare_image(self, data, extension):
        """Return the bytes to store for one embedded image.

        The bytes are passed through untouched unless the caller asked for
        a ``max_image_edge`` - or the picture is too large for the editor
        to decode at all, in which case keeping the original would only
        spend room on a picture that can never be shown.
        """
        size = _pixel_size(data)
        if size is None:
            return data, extension
        width, height = size
        longest = max(width, height)
        limit = self.max_image_edge or 0
        if width * height > MAX_IMAGE_PIXELS:
            shrink = longest * (MAX_IMAGE_PIXELS / (width * height)) ** 0.5
            limit = min(limit, shrink) if limit else shrink
            logger.info('%s is %dx%d, scaling it down to be displayable',
                        extension, width, height)
        if not limit or longest <= limit:
            return data, extension
        ratio = limit / longest
        target = (max(1, int(width * ratio)), max(1, int(height * ratio)))
        self.report.images_scaled += 1
        return _scale_image(data, target, extension)

    # ── Tables ────────────────────────────────────────────────────

    def _table_html(self, table):
        rows = []
        for row in table.rows:
            cells = []
            seen = set()
            for cell in row.cells:
                element = getattr(cell, '_tc', None)
                if element is not None:
                    marker = id(element)
                    if marker in seen:
                        # A horizontally merged cell is reported once per
                        # grid column it covers; only the first is written.
                        continue
                    seen.add(marker)
                span = ''
                try:
                    grid_span = int(cell.grid_span or 1)
                except (AttributeError, TypeError, ValueError):
                    grid_span = 1
                if grid_span > 1:
                    span = f' colspan="{grid_span}"'
                cells.append(f'<td{span}>{self._cell_html(cell)}</td>')
            rows.append(f'<tr>{"".join(cells)}</tr>')
        if not rows:
            return ''
        return f'<table border="1">{"".join(rows)}</table>'

    def _cell_html(self, cell):
        chunks = []
        lists = _ListWriter()
        for paragraph in cell.paragraphs:
            info = self._numbering_of(paragraph)
            if info is None:
                chunks.append(lists.close())
                chunks.append(self._paragraph_html(paragraph))
            else:
                level, list_kind, ordered, num_id = info
                content = self._paragraph_html(paragraph, in_list=True)
                chunks.append(lists.item(level, list_kind, ordered, num_id,
                                         content))
        chunks.append(lists.close())
        return ''.join(chunks)
