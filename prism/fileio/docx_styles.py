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

"""Word values translated into the markup the note editor understands.

Word stores formatting in WordprocessingML, the editor wants HTML and
CSS.  Every table that maps one onto the other lives here, so
``import_docx.py`` only has to walk the document tree.  The values come
from the published OOXML specification and from the styles Word writes
for its own built-in formats; nothing here is taken from Word itself.
"""


# ── Units ──────────────────────────────────────────────────────────────

#: English Metric Units per CSS pixel. 914400 EMU = 1 inch = 96 px, so
#: Word's indents and image sizes divide straight into pixels.
EMU_PER_PX = 9525


def emu_to_px(value):
    """Convert a Word length (EMU) into CSS pixels."""
    if value is None:
        return None
    try:
        return float(value) / EMU_PER_PX
    except (TypeError, ValueError):
        return None


def _number(value):
    """Format a pixel count without trailing zeros (96.0 -> '96')."""
    if value is None:
        return None
    rounded = round(float(value), 1)
    if abs(rounded - round(rounded)) < 0.05:
        return str(int(round(rounded)))
    return str(rounded)


def css_length(pixels):
    """Format a pixel count as a CSS length (96 -> '96px')."""
    value = _number(pixels)
    return None if value is None else f'{value}px'


def css_point_size(points):
    """Format a point size as CSS wants it (12 -> '12pt')."""
    value = _number(points)
    return None if value is None else f'{value}pt'


def css_color(rgb):
    """Format an RGBColor as a CSS color ('#222222')."""
    if rgb is None:
        return None
    text = str(rgb).strip().lstrip('#')
    if len(text) == 6:
        return f'#{text.lower()}'
    return None


def font_stack(names):
    """Build a CSS font list out of Word's ascii/eastAsia font pair.

    Word keeps one font for Latin text and another one for CJK, so a run
    that says "Arial" for Latin still asks for 微软雅黑 for Chinese.  CSS
    has a single list, and dropping the CJK font would render the Chinese
    half of a note in the wrong face.
    """
    unique = []
    for name in names:
        clean = (name or '').strip().strip('"\'')
        if clean and clean not in unique:
            unique.append(clean)
    if not unique:
        return None
    return ', '.join(f"'{name}'" for name in unique)


# ── Paragraph styles ───────────────────────────────────────────────────

#: Word's built-in heading styles, keyed by the name the file carries.
#: Chinese Word stores '标题 1' where an English one stores 'heading 1', so
#: both spellings are listed; lookups are case-insensitive.
HEADING_LEVELS = {'title': 1, '标题': 1}
for _level in range(1, 10):
    HEADING_LEVELS[f'heading {_level}'] = min(_level, 6)
    HEADING_LEVELS[f'标题 {_level}'] = min(_level, 6)
del _level

#: Word marks outline level 9 as "body text" - the opposite of a heading.
BODY_TEXT_OUTLINE_LEVEL = 9


def heading_level(style):
    """Return 1-6 when this paragraph style is a heading, else ``None``."""
    if style is None:
        return None
    name = (getattr(style, 'name', '') or '').strip().casefold()
    if name in HEADING_LEVELS:
        return HEADING_LEVELS[name]
    return _heading_from_outline_level(style)


def _heading_from_outline_level(style):
    """Read the outline level off a style, and off the styles it is based on.

    ``w:outlineLvl`` is the language independent half of a heading: a
    translation of Word ships with translated style *names*, but the
    outline level stays the same number.
    """
    seen = set()
    while style is not None and id(style) not in seen:
        seen.add(id(style))
        level = _style_outline_level(style)
        if level is not None:
            if level >= BODY_TEXT_OUTLINE_LEVEL:
                return None
            return min(level + 1, 6)
        style = getattr(style, 'base_style', None)
    return None


def _style_outline_level(style):
    from docx.oxml.ns import qn

    element = getattr(style, 'element', None)
    if element is None:
        return None
    properties = element.find(qn('w:pPr'))
    if properties is None:
        return None
    node = properties.find(qn('w:outlineLvl'))
    if node is None:
        return None
    try:
        return int(node.get(qn('w:val')))
    except (TypeError, ValueError):
        return None


# ── Paragraph properties ───────────────────────────────────────────────

#: ``paragraph.alignment`` (WD_ALIGN_PARAGRAPH) -> CSS ``text-align``.
#: Word's stretching variants (distribute, justify medium/high/low, Thai)
#: have no HTML equivalent, so they land on plain justification.
ALIGNMENTS = {
    0: 'left',
    1: 'center',
    2: 'right',
    3: 'justify',
    4: 'justify',
    5: 'justify',
    7: 'justify',
    8: 'justify',
    9: 'justify',
}


# ── Lists ──────────────────────────────────────────────────────────────

#: ``w:numFmt`` values that draw a bullet instead of a number.
BULLET_FORMATS = frozenset({'bullet', 'picture'})

#: ``w:numFmt`` values that mean "this paragraph carries no marker".
UNMARKED_FORMATS = frozenset({'none', 'bulletNone', 'clear'})

#: Numbering formats -> the ``type`` attribute of an ordered list.  HTML
#: only knows 1/a/A/i/I, so every other counting scheme keeps its position
#: and direction (Chinese 一、二、三 counts like 1, 2, 3, ...).
ORDERED_TYPES = {
    'decimal': '1',
    'decimalZero': '1',
    'decimalEnclosedCircle': '1',
    'decimalEnclosedCircleChinese': '1',
    'decimalEnclosedFullstop': '1',
    'decimalEnclosedParen': '1',
    'decimalFullWidth': '1',
    'decimalFullWidth2': '1',
    'decimalHalfWidth': '1',
    'cardinalText': '1',
    'ordinal': '1',
    'ordinalText': '1',
    'chineseCounting': '1',
    'chineseCountingThousand': '1',
    'chineseLegalSimplified': '1',
    'ideographDigital': '1',
    'ideographTraditional': '1',
    'ideographLegalTraditional': '1',
    'countingThousand': '1',
    'japaneseCounting': '1',
    'koreanCounting': '1',
    'koreanDigital': '1',
    'koreanLegal': '1',
    'vietnameseCounting': '1',
    'hebrew1': '1',
    'lowerLetter': 'a',
    'upperLetter': 'A',
    'russianLower': 'a',
    'russianUpper': 'A',
    'arabicAlpha': 'a',
    'arabicAbjad': 'a',
    'aiueo': 'a',
    'aiueoFullWidth': 'a',
    'thaiLetters': 'a',
    'lowerRoman': 'i',
    'upperRoman': 'I',
    'iroha': 'i',
    'irohaFullWidth': 'i',
}


def list_tag(number_format):
    """Return ``'ul'``, ``'ol'`` or ``None`` for a ``w:numFmt`` value.

    ``None`` means the paragraph is not a list item: either numbering was
    switched off for it, or the document stores no format at all.  An
    unknown format still counts as an ordered list, because Word's own
    default numbering format is decimal.
    """
    if number_format is None:
        return 'ol'
    name = str(number_format)
    if name in UNMARKED_FORMATS:
        return None
    if name in BULLET_FORMATS:
        return 'ul'
    return 'ol'


def ordered_type(number_format):
    """Return the ``<ol type=...>`` value for a ``w:numFmt`` value."""
    return ORDERED_TYPES.get(str(number_format or ''))


# ── Character properties ───────────────────────────────────────────────

#: ``run.font.highlight_color`` (WD_COLOR_INDEX) -> CSS background.  These
#: are the colours of Word's highlighter pen palette.
HIGHLIGHT_COLORS = {
    1: '#000000',    # black
    2: '#0000ff',    # blue
    3: '#00ffff',    # turquoise
    4: '#00ff00',    # bright green
    5: '#ff00ff',    # pink
    6: '#ff0000',    # red
    7: '#ffff00',    # yellow
    8: '#ffffff',    # white
    9: '#000080',    # dark blue
    10: '#008080',   # teal
    11: '#008000',   # green
    12: '#800080',   # violet
    13: '#800000',   # dark red
    14: '#808000',   # dark yellow
    15: '#808080',   # gray 50%
    16: '#c0c0c0',   # gray 25%
}


def highlight_color(value):
    """Return the CSS colour of a highlight value, or ``None``."""
    if value is None:
        return None
    try:
        return HIGHLIGHT_COLORS.get(int(value))
    except (TypeError, ValueError):
        return None
