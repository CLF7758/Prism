"""Text decoding for the Wavefront/FBX readers, with the ambiguity kept.

A model file's material names are identity material: ``usemtl Wood`` must bind
to the ``newmtl Wood`` the file declares, and two materials must not collapse
into one because their bytes decode to the same replacement characters.

The rule this module implements:

* a UTF-8 BOM, a UTF-16 BOM or a file that decodes as strict UTF-8 is
  **certain**;
* anything else is **ambiguous**: the bytes are decoded with the best-supported
  candidate for display, every other candidate that also decodes cleanly is
  reported, and the caller must build identity from the **original bytes**, not
  from the display string;
* bytes that decode under no candidate keep a lossy, explicitly marked display
  string (U+FFFD) and are reported as damaged.

Nothing here rewrites or drops the original bytes.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: BOMs that decide the encoding on their own.
_UTF8_BOM = b'\xef\xbb\xbf'
_UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')

#: Legacy candidates tried, in preference order, only when the file is not
#: certain.  Latin-1 is deliberately *not* a candidate: it decodes any byte
#: sequence, so accepting it would mean "guessing" and would hide damage.
LEGACY_CANDIDATES = ('gb18030', 'big5', 'shift_jis', 'cp1252')

#: How much of the raw bytes an identity digest covers.  Enough to separate two
#: names that decode alike, short enough to keep a payload readable.
IDENTITY_DIGEST_LENGTH = 10


@dataclass(frozen=True)
class DecodedText:
    """One decoding decision, with everything needed to audit it."""

    text: str
    encoding: str
    certain: bool
    lossy: bool = False
    candidates: tuple = ()
    notes: tuple = ()

    @property
    def ambiguous(self) -> bool:
        return not self.certain

    def identity(self, raw: bytes) -> str:
        """A stable identity for one raw value, independent of the decoding.

        A certain decoding lets the text be the identity (readable, stable).
        An ambiguous one must not: two different byte strings can decode to the
        same replacement characters, so the digest of the bytes is used instead.
        """
        digest = hashlib.sha256(raw or b'').hexdigest()[:IDENTITY_DIGEST_LENGTH]
        if self.certain and not self.lossy:
            return self.text
        return f'{self.text}~{digest}'


def decode_text(data: bytes) -> DecodedText:
    """Decode a text container, keeping the provenance of the decision."""
    raw = bytes(data or b'')
    if raw.startswith(_UTF8_BOM):
        return DecodedText(raw.decode('utf-8-sig'), 'utf-8-sig', True)
    if raw[:2] in _UTF16_BOMS:
        try:
            return DecodedText(raw.decode('utf-16'), 'utf-16', True)
        except UnicodeDecodeError as error:
            return DecodedText(raw.decode('utf-8', 'replace'), 'utf-16-broken',
                               False, True, notes=(f'UTF-16 BOM 但解码失败：{error}',))
    try:
        return DecodedText(raw.decode('utf-8'), 'utf-8', True)
    except UnicodeDecodeError:
        pass
    decodable = []
    for encoding in LEGACY_CANDIDATES:
        try:
            decodable.append((encoding, raw.decode(encoding)))
        except (UnicodeDecodeError, LookupError):
            continue
    if not decodable:
        note = ('文件不是 UTF-8，也没有任何候选旧编码能解码；'
                '名称按替换字符显示，身份按原始字节')
        return DecodedText(raw.decode('utf-8', 'replace'), 'damaged', False, True,
                           notes=(note,))
    encoding, text = decodable[0]
    others = tuple(name for name, _value in decodable[1:])
    note = (f'文件不是 UTF-8；按 {encoding} 显示'
            + (f'，但 {"/".join(others)} 同样能解码' if others else '')
            + '；名称身份按原始字节')
    return DecodedText(text, encoding, False, False, candidates=tuple(
        name for name, _value in decodable), notes=(note,))


def decode_snippet(raw: bytes) -> DecodedText:
    """Decode one small value (a name, a path) on its own.

    A snippet cannot be told apart from any single-byte encoding by itself, so
    it is decoded the same way and stays ``certain=False`` unless it is pure
    ASCII -- which is the common case and worth being certain about.
    """
    raw = bytes(raw or b'')
    if all(byte < 0x80 for byte in raw):
        return DecodedText(raw.decode('ascii'), 'ascii', True)
    return decode_text(raw)


def line_bytes(data: bytes) -> list:
    """The file's lines as raw bytes, with continuations already joined.

    Working on bytes keeps the byte-level identity of names and paths: the
    display string may be lossy, the key is not.
    """
    lines = []
    buffer = b''
    for raw in bytes(data or b'').splitlines():
        stripped = raw.rstrip()
        if stripped.endswith(b'\\'):
            buffer += stripped[:-1] + b' '
            continue
        lines.append(buffer + stripped)
        buffer = b''
    if buffer.strip():
        lines.append(buffer)
    return lines


def split_keyword(line: bytes) -> tuple:
    """``(lower-case ASCII keyword, raw remainder)`` of one line."""
    head, _, rest = bytes(line).partition(b' ')
    return head.decode('ascii', 'replace').strip().lower(), rest


def byte_identity(raw: bytes) -> str:
    """The identity of one raw value, when the decoding cannot be trusted."""
    return hashlib.sha256(bytes(raw or b'')).hexdigest()[:IDENTITY_DIGEST_LENGTH]


@dataclass(frozen=True)
class Line:
    """One logical line: its keyword, its raw bytes and its display text.

    Everything downstream works on this triple, which is what makes the two
    requirements compatible: a ``usemtl`` is matched by **bytes**, while the
    user sees text.  A UTF-16 container is read as text (the BOM makes it
    certain) and its value bytes are re-encoded as UTF-8, so a UTF-16 value and
    a UTF-8 value with the same characters share one identity.
    """

    keyword: str
    raw: bytes
    text: str
    certain: bool = True

    @property
    def identity(self) -> str:
        if self.certain:
            return self.text
        # ``name~digest``: readable enough to debug, unique enough that two
        # values which decode to the same replacement characters stay apart.
        return f'{self.text}~{byte_identity(self.raw)}'


def lines(data: bytes, document: Optional[DecodedText] = None) -> list:
    """Split a text container into :class:`Line`s, joining continuations.

    A UTF-16 container cannot be split on bytes: every second byte is a NUL.  It
    is split as text and its values are normalised to UTF-8 bytes, so the same
    characters get the same identity whichever container carried them.
    """
    document = document or decode_text(data)
    if document.certain and document.encoding.startswith('utf-16'):
        return _lines_from_text(document.text)
    result = []
    for raw in line_bytes(data):
        keyword, remainder = split_keyword(raw)
        value = remainder.strip()
        decoded = decode_snippet(value)
        result.append(Line(keyword=keyword, raw=value, text=decoded.text,
                           certain=decoded.certain and not decoded.lossy))
    return result


def _lines_from_text(text: str) -> list:
    result = []
    buffer = ''
    for raw in str(text).splitlines():
        stripped = raw.rstrip()
        if stripped.endswith('\\'):
            buffer += stripped[:-1] + ' '
            continue
        for joined in [buffer + stripped]:
            keyword, _, remainder = joined.partition(' ')
            value = remainder.strip()
            result.append(Line(keyword=keyword.strip().lower(),
                               raw=value.encode('utf-8'), text=value,
                               certain=True))
        buffer = ''
    if buffer.strip():
        keyword, _, remainder = buffer.partition(' ')
        value = remainder.strip()
        result.append(Line(keyword=keyword.strip().lower(),
                           raw=value.encode('utf-8'), text=value, certain=True))
    return result
