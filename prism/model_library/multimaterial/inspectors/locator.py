"""Resolve the file locators a model states, without guessing anything.

A texture reference in a model file is a string.  Turning that string into a
resource is a separate, auditable step:

* a relative reference is resolved against the directory of the file that
  stated it, never against a process working directory;
* a reference that leaves the package (an absolute path, a remote URL) is
  reported as such instead of being silently accepted;
* the identity of a resource is the SHA-256 of its bytes, so two files with the
  same name are two resources and one file copied twice is one;
* nothing is ever written, downloaded or decompressed without an explicit
  size limit.
"""
from __future__ import annotations

import base64
import binascii
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..catalog import ResourceRef, resource_id_of

#: An embedded image larger than this is not hashed.  Reading it would cost
#: more memory than a preview is worth; the locator and the size are reported
#: instead of a digest.
MAX_INSPECT_BYTES = 64 * 1024 * 1024

#: Extensions that cannot carry a texture this application can read.
IMAGE_SUFFIXES = frozenset({
    '.png', '.jpg', '.jpeg', '.bmp', '.tga', '.tif', '.tiff', '.gif', '.webp',
    '.exr', '.hdr', '.dds', '.psd',
})


@dataclass(frozen=True)
class Locator:
    """One reference as the file stated it, plus what we could make of it."""

    original: str
    path: Optional[str] = None
    absolute: bool = False
    remote: bool = False
    data_uri: bool = False
    outside_package: bool = False

    @property
    def resolved(self) -> bool:
        return self.path is not None


def is_absolute_reference(reference: str) -> bool:
    text = str(reference or '')
    if not text:
        return False
    if text.startswith(('/', '\\')):
        return True
    return len(text) > 1 and text[1] == ':'


def is_remote_reference(reference: str) -> bool:
    text = str(reference or '').lower()
    return text.startswith(('http://', 'https://', 'ftp://', '//'))


def decode_uri(reference: str) -> str:
    """The local path a URI states, with percent escapes resolved."""
    parsed = urllib.parse.urlparse(str(reference))
    if parsed.scheme == 'file':
        local = urllib.parse.unquote(parsed.path)
        if os.name == 'nt' and local.startswith('/') and len(local) > 2 and local[2] == ':':
            local = local[1:]
        return local
    return urllib.parse.unquote(str(reference))


def resolve_locator(reference: str, base_dir) -> Locator:
    """Resolve one reference against the directory that stated it."""
    text = str(reference or '').strip().strip('"')
    if not text:
        return Locator(original=text)
    if text.startswith('data:'):
        return Locator(original=text, data_uri=True)
    if is_remote_reference(text):
        return Locator(original=text, remote=True)
    local = decode_uri(text)
    absolute = is_absolute_reference(local)
    base = Path(base_dir) if base_dir else None
    candidate = Path(local) if absolute else (base / local if base is not None else None)
    if candidate is None:
        return Locator(original=text, absolute=absolute)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    exists = resolved.is_file()
    outside = False
    if base is not None and exists:
        try:
            resolved.relative_to(Path(base).resolve())
        except ValueError:
            outside = True
    return Locator(original=text, path=str(resolved) if exists else None,
                   absolute=absolute, outside_package=outside)


def read_reference(reference: str, base_dir, *,
                   limit: int = MAX_INSPECT_BYTES) -> tuple:
    """``(ResourceRef, note)`` for one texture reference.

    ``note`` is a human-readable sentence when the bytes were not read; the
    caller records it as unresolved instead of pretending the texture is fine.
    """
    locator = resolve_locator(reference, base_dir)
    if locator.data_uri:
        return _read_data_uri(locator, limit=limit)
    if locator.remote:
        return (ResourceRef(locator=locator.original, remote=True),
                f'远程引用未下载：{locator.original}')
    if not locator.resolved:
        # An absolute path that no longer resolves is a relocation problem, not
        # a broken model: the flags say which one it is.
        return (ResourceRef(locator=locator.original, missing=True,
                            absolute=locator.absolute),
                f'找不到引用的文件：{locator.original}')
    path = Path(locator.path)
    try:
        size = path.stat().st_size
    except OSError:
        return (ResourceRef(locator=locator.original, missing=True,
                            absolute=locator.absolute),
                f'无法读取引用的文件：{locator.original}')
    if size > limit:
        return (ResourceRef(locator=locator.original, size_bytes=size,
                            absolute=locator.absolute),
                f'引用的文件过大，未计算摘要：{locator.original}（{size} 字节）')
    try:
        data = path.read_bytes()
    except OSError as error:
        return (ResourceRef(locator=locator.original, missing=True,
                            absolute=locator.absolute),
                f'读取失败：{locator.original}（{error}）')
    note = ''
    if locator.outside_package:
        note = f'引用的文件在包外，搬迁后无法自动修复：{locator.original}'
    return (ResourceRef(resource_id=resource_id_of(data), locator=locator.original,
                        size_bytes=len(data), absolute=locator.absolute), note)


def read_bytes(reference: str, base_dir, *, limit: int = MAX_INSPECT_BYTES) -> Optional[bytes]:
    """The bytes of an embedded (``data:``) reference, or ``None``."""
    text = str(reference or '').strip()
    if not text.startswith('data:'):
        return None
    header, _, payload = text.partition(',')
    if ';base64' not in header:
        return None
    if len(payload) * 3 // 4 > limit:
        return None
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def _read_data_uri(locator: Locator, *, limit: int) -> tuple:
    text = locator.original
    header, _, payload = text.partition(',')
    if ';base64' not in header:
        return (ResourceRef(locator=_short_data_uri(text), embedded=True),
                f'内嵌数据不是 base64，未读取：{_short_data_uri(text)}')
    if len(payload) * 3 // 4 > limit:
        return (ResourceRef(locator=_short_data_uri(text), embedded=True),
                f'内嵌数据过大，未计算摘要：{_short_data_uri(text)}')
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return (ResourceRef(locator=_short_data_uri(text), embedded=True),
                f'内嵌数据无法解码：{_short_data_uri(text)}')
    return (ResourceRef(resource_id=resource_id_of(data),
                        locator=_short_data_uri(text), embedded=True,
                        size_bytes=len(data)), '')


def _short_data_uri(text: str) -> str:
    header, _, _payload = text.partition(',')
    return header + ',…'


def relative_locator(path, base_dir) -> str:
    """A package-relative locator for a file inside the package."""
    try:
        return Path(path).resolve().relative_to(Path(base_dir).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)
