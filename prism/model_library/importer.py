"""Import a model and everything it needs into the project.

The importer does three things and refuses to fake any of them:

* it **reads** the model file and reports the dependencies the file itself
  declares -- never a guess made from file names;
* it copies the model and its dependencies into the project and verifies every
  byte against a SHA-256 that was computed from the copy;
* it commits the record and its originals in **one** transaction, so a
  cancelled or failed import leaves no "ready" asset behind.

Format support and what "declared dependency" means per format:

``OBJ``
    ``mtllib`` directives, then the ``map_*``/``norm``/``disp``/``decal``
    texture references of every material library it names.  The file name is
    the rest of the line, so spaces, case and directory levels survive.
``glTF``
    ``buffers[].uri`` and ``images[].uri``.  ``data:`` URIs are embedded and
    are not files; ``http(s):`` URIs are reported, never downloaded.
``GLB``
    the JSON chunk of the container, plus the presence of a BIN chunk.  The
    original bytes are kept exactly as they are.
``FBX``
    the ``Texture``/``Video``/``Media`` objects of the file, read from the
    binary or ASCII container.  A reference the file does not declare is not
    added; a reference that cannot be resolved is reported as unresolved.

The layout inside the project keeps the relative relations of the original
directory: the package root is the deepest directory that contains the model
**and** all of its found dependencies, so ``../textures/a.png`` still resolves
after the project moved.  A dependency that lives on another volume cannot
keep that relation; it is stored under ``_external/<id>/`` and the manifest
records the original absolute reference.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .events import (EVENT_IMPORT_COMMITTED, EVENT_IMPORT_COPIED,
                     EVENT_IMPORT_DEPENDENCIES_DETECTED,
                     EVENT_IMPORT_VALIDATED, record_model_event)
from .contracts import MODEL_EXTENSIONS
from .project_storage import (
    ModelFileDetail,
    ModelImportCompletedEvent,
    ModelImportFailure,
    ModelImportFailedEvent,
    ModelImportResult,
    ModelJobPhaseEvent,
    ModelProjectContext,
    ModelStorageError,
    ModelOperationCancelled,
    sha256_of,
    validate_model_records,
)
from .repository import ModelRepositoryAdapter

#: The role a file plays inside a model.
ROLE_MAIN = 'main'
ROLE_MATERIAL = 'material'
ROLE_TEXTURE = 'texture'
ROLE_BUFFER = 'buffer'
ROLE_IMAGE = 'image'
ROLE_UNKNOWN = 'unknown'

#: How a declared reference could be resolved.
RES_RELATIVE = 'relative'
RES_ABSOLUTE = 'absolute'
RES_EMBEDDED = 'embedded'
RES_MISSING = 'missing'
RES_UNRESOLVED = 'unresolved'

#: Texture keywords of a Wavefront material library that name a file.  They
#: are stored lower case and compared against a lower-cased keyword, because
#: ``map_Kd`` is spelled with a capital in most files and with a small one in
#: others.
MTL_TEXTURE_KEYWORDS = (
    'map_kd', 'map_ka', 'map_ks', 'map_ke', 'map_ns', 'map_d', 'map_bump',
    'map_kn', 'map_pr', 'map_pm', 'map_ps', 'map_refl', 'bump', 'norm', 'disp',
    'displacement', 'decal', 'refl',
)

#: Arity of the options a material library may put in front of a file name.
#: Only the options that take a value are listed; the rest are flags.
MTL_OPTION_ARITY = {
    '-blendu': 1, '-blendv': 1, '-boost': 1, '-texres': 1, '-clamp': 1,
    '-bm': 1, '-imfchan': 1, '-type': 1, '-cc': 1,
    '-mm': 2, '-o': 3, '-s': 3, '-t': 3,
}


@dataclass(frozen=True)
class DetectedReference:
    """One dependency the model file itself declares."""

    reference: str
    role: str
    resolution: str
    path: Optional[str] = None
    detail: str = ''

    @property
    def is_file(self) -> bool:
        return self.path is not None


# ---------------------------------------------------------------------------
# format readers
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    """Read a text file without guessing an encoding it may not have."""
    data = Path(path).read_bytes()
    for encoding in ('utf-8-sig', 'utf-8', 'gbk', 'latin-1'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', 'replace')


def parse_obj_references(path: Path) -> tuple:
    """The material libraries an OBJ file names."""
    references = []
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != 'mtllib':
            continue
        remainder = parts[1].strip()
        # The rest of the line is the file name.  A name with spaces is only
        # split when the whole line does not name an existing file.
        candidates = [remainder]
        if len(remainder.split()) > 1:
            candidates.extend(remainder.split())
        chosen = remainder
        for candidate in candidates:
            resolved = _resolve_relative(path, candidate)
            if resolved is not None:
                chosen = candidate
                break
        references.append(DetectedReference(
            reference=chosen, role=ROLE_MATERIAL, resolution=RES_RELATIVE,
            path=_resolve_relative(path, chosen),
            detail='mtllib'))
    return tuple(references)


def parse_mtl_references(path: Path) -> tuple:
    """The texture files a material library names."""
    references = []
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        keyword, _, remainder = stripped.partition(' ')
        if keyword.lower() not in MTL_TEXTURE_KEYWORDS:
            continue
        name = _mtl_file_name(remainder.strip())
        if not name:
            continue
        absolute = _is_absolute_reference(name)
        resolved = _resolve_reference(path, name)
        references.append(DetectedReference(
            reference=name, role=ROLE_TEXTURE,
            resolution=RES_ABSOLUTE if absolute else RES_RELATIVE,
            path=resolved, detail=keyword.lower()))
    return tuple(references)


def _mtl_file_name(remainder: str) -> str:
    """The file name of a texture line, with its spaces intact."""
    tokens = remainder.split()
    index = 0
    while index < len(tokens) and tokens[index].startswith('-'):
        option = tokens[index].lower()
        arity = MTL_OPTION_ARITY.get(option, 0)
        index += 1 + arity
    if index >= len(tokens):
        return ''
    # Everything from the first non-option token is the name.
    remainder_tokens = remainder.split(None, index)
    if not remainder_tokens:
        return ''
    return ' '.join(remainder_tokens).strip()


def parse_gltf_references(path: Path) -> tuple:
    """The external files a glTF document names."""
    try:
        document = json.loads(read_text(path))
    except ValueError as error:
        raise ModelStorageError(f'The glTF document is not valid JSON: {error}')
    if not isinstance(document, dict):
        raise ModelStorageError('The glTF document is not an object')
    references = []
    for index, buffer in enumerate(document.get('buffers') or ()):
        if not isinstance(buffer, dict):
            continue
        uri = buffer.get('uri')
        if uri is None:
            continue
        references.append(_uri_reference(path, str(uri), ROLE_BUFFER,
                                         f'buffers[{index}]'))
    for index, image in enumerate(document.get('images') or ()):
        if not isinstance(image, dict):
            continue
        uri = image.get('uri')
        if uri is None:
            # An image without a uri lives inside a buffer view.
            continue
        references.append(_uri_reference(path, str(uri), ROLE_IMAGE,
                                         f'images[{index}]'))
    return tuple(references)


def _uri_reference(path: Path, uri: str, role: str, detail: str) -> DetectedReference:
    if uri.startswith('data:'):
        return DetectedReference(reference=uri, role=role,
                                 resolution=RES_EMBEDDED, detail=detail)
    if uri.startswith(('http://', 'https://', 'ftp://')):
        # Never download anything: the reference is reported, not fetched.
        return DetectedReference(reference=uri, role=role,
                                 resolution=RES_UNRESOLVED, detail=detail)
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme == 'file':
        local = urllib.parse.unquote(parsed.path)
        if os.name == 'nt' and local.startswith('/') and len(local) > 2 and local[2] == ':':
            local = local[1:]
        return DetectedReference(reference=uri, role=role,
                                 resolution=RES_ABSOLUTE if _is_absolute_reference(local)
                                 else RES_RELATIVE,
                                 path=_resolve_reference(path, local), detail=detail)
    decoded = urllib.parse.unquote(uri)
    absolute = _is_absolute_reference(decoded)
    return DetectedReference(reference=uri, role=role,
                             resolution=RES_ABSOLUTE if absolute else RES_RELATIVE,
                             path=_resolve_reference(path, decoded), detail=detail)


def parse_glb_references(path: Path) -> tuple:
    """The external files a GLB container names, plus its BIN chunk."""
    with Path(path).open('rb') as handle:
        header = handle.read(12)
        if len(header) < 12:
            raise ModelStorageError('The GLB file is shorter than its header')
        magic, version, _length = struct.unpack('<4sII', header)
        if magic != b'glTF':
            raise ModelStorageError('The file is not a GLB container')
        if version != 2:
            raise ModelStorageError(f'Unsupported GLB version {version}')
        document = None
        has_bin = False
        while True:
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_length, chunk_type = struct.unpack('<II', chunk_header)
            data = handle.read(chunk_length)
            if len(data) < chunk_length:
                raise ModelStorageError('The GLB container is truncated')
            if chunk_type == 0x4E4F534A and document is None:
                document = json.loads(data.decode('utf-8'))
            elif chunk_type == 0x004E4942:
                has_bin = True
    if document is None:
        raise ModelStorageError('The GLB container has no JSON chunk')
    references = []
    for index, buffer in enumerate(document.get('buffers') or ()):
        if not isinstance(buffer, dict):
            continue
        uri = buffer.get('uri')
        if uri is None:
            if not has_bin:
                references.append(DetectedReference(
                    reference='', role=ROLE_BUFFER, resolution=RES_MISSING,
                    detail=f'buffers[{index}] has no uri and the container has no BIN chunk'))
            continue
        references.append(_uri_reference(path, str(uri), ROLE_BUFFER,
                                         f'buffers[{index}]'))
    for index, image in enumerate(document.get('images') or ()):
        if not isinstance(image, dict):
            continue
        uri = image.get('uri')
        if uri is None:
            continue
        references.append(_uri_reference(path, str(uri), ROLE_IMAGE,
                                         f'images[{index}]'))
    return tuple(references)


# -- FBX -------------------------------------------------------------------

FBX_MAGIC = b'Kaydara FBX Binary  \x00'

#: How deep the reader may descend and how many records it may read.  A file
#: that breaks the container's own promises is reported as unreadable instead
#: of being walked for ever.
FBX_MAX_DEPTH = 64
FBX_MAX_RECORDS = 2_000_000

#: A string property longer than this is not read: no dependency name is that
#: long, and copying it would only pull embedded content into memory.
FBX_MAX_STRING = 64 * 1024

#: The property names an FBX object uses to state where a texture lives.
FBX_PATH_KEYS = ('RelativeFilename', 'FileName', 'Reference')


@dataclass(frozen=True)
class _FbxProperty:
    """One property of an FBX record.  Arrays keep their type, not their data."""

    kind: str
    value: object = None


def parse_fbx_references(path: Path) -> tuple:
    """The texture and video references an FBX file declares.

    Only what the file states is reported.  A model whose FBX declares no
    texture objects has no textures here -- the file names are never used to
    guess one.  Binary and ASCII containers are both read; a container that
    cannot be walked is reported as unreadable instead of being guessed at.
    """
    with Path(path).open('rb') as handle:
        head = handle.read(27)
    if len(head) < 27:
        raise ModelStorageError('The FBX file is shorter than its header')
    if head.startswith(FBX_MAGIC):
        return _parse_binary_fbx(path)
    return _parse_ascii_fbx(path)


def _parse_binary_fbx(path: Path) -> tuple:
    size = Path(path).stat().st_size
    with Path(path).open('rb') as handle:
        header = handle.read(27)
        version = struct.unpack('<I', header[23:27])[0]
        records = _BinaryFbxReader(handle, size, version).read()
    references = []
    for node in records:
        _collect_fbx_node(node, references)
    return tuple(_one_reference_per_object(references))


class _BinaryFbxReader:
    """Walk the node tree of a binary FBX container.

    Every offset the file states is checked against the real file size and
    against the current position, so a truncated or hostile container stops
    the walk.  The property list of a record has a declared length, so the
    reader can always return to a record boundary even when it had to skip a
    large array it does not need.
    """

    def __init__(self, handle, size: int, version: int):
        self.handle = handle
        self.size = int(size)
        self.version = int(version)
        # 7.5 stores 64-bit offsets; earlier versions store 32-bit ones.
        self.wide = int(version) >= 7500
        # Some 7.0-7.3 exporters already use the encoded array header.
        self.modern_arrays = int(version) >= 7400
        self.header_size = 25 if self.wide else 13
        self.records = 0

    def read(self) -> list:
        """The top-level records of the container."""
        self.handle.seek(27)
        nodes, _terminated = self._read_list(self.size, 0)
        return nodes

    # -- container ----------------------------------------------------

    def _read_list(self, boundary: int, depth: int) -> tuple:
        nodes = []
        limit = min(int(boundary), self.size)
        while True:
            position = self.handle.tell()
            if position + self.header_size > limit:
                return nodes, False
            header = self.handle.read(self.header_size)
            if len(header) < self.header_size:
                return nodes, False
            if header == b'\x00' * self.header_size:
                return nodes, True
            node = self._read_node(header, position, depth)
            if node is None:
                return nodes, False
            nodes.append(node)
            self.records += 1
            if self.records > FBX_MAX_RECORDS:
                return nodes, False

    def _read_node(self, header: bytes, position: int, depth: int):
        if self.wide:
            end, count, property_length = struct.unpack('<QQQ', header[:24])
            name_length = header[24]
        else:
            end, count, property_length = struct.unpack('<III', header[:12])
            name_length = header[12]
        end, count, property_length = int(end), int(count), int(property_length)
        start = position + self.header_size
        if end and (end <= position or end > self.size):
            # The record's own end offset cannot be trusted; stop here rather
            # than seek backwards and read the same bytes again.
            return None
        if start + name_length + property_length > self.size:
            return None
        name = self.handle.read(name_length).decode('utf-8', 'replace')
        properties_start = start + name_length
        self.handle.seek(properties_start)
        properties = self._read_properties(count, property_length)
        # The declared property length is authoritative: whatever a property
        # parse recovered, the children start right after the property list.
        self.handle.seek(properties_start + property_length)
        children = []
        if depth < FBX_MAX_DEPTH:
            children, _terminated = self._read_list(end or self.size, depth + 1)
        if end:
            self.handle.seek(end)
        return _FbxNode(name, properties, children, end)

    # -- properties ---------------------------------------------------

    def _read_properties(self, count: int, length: int) -> list:
        limit = self.handle.tell() + length
        values = []
        for _ in range(count):
            if self.handle.tell() >= limit:
                break
            try:
                value = self._read_property(limit)
            except (struct.error, ValueError, OSError):
                break
            if value is None:
                break
            values.append(value)
        return values

    def _read(self, size: int) -> bytes:
        data = self.handle.read(size)
        if len(data) != size:
            raise ValueError('The FBX container is truncated')
        return data

    def _read_property(self, limit: int):
        kind = self.handle.read(1)
        if not kind:
            return None
        kind = kind.decode('ascii', 'replace')
        if kind == 'Y':
            return _FbxProperty(kind, struct.unpack('<h', self._read(2))[0])
        if kind == 'C':
            return _FbxProperty(kind, bool(self._read(1)[0]))
        if kind == 'I':
            return _FbxProperty(kind, struct.unpack('<i', self._read(4))[0])
        if kind == 'F':
            return _FbxProperty(kind, struct.unpack('<f', self._read(4))[0])
        if kind == 'D':
            return _FbxProperty(kind, struct.unpack('<d', self._read(8))[0])
        if kind == 'L':
            return _FbxProperty(kind, struct.unpack('<q', self._read(8))[0])
        if kind in 'fdlib':
            return self._skip_array(kind, limit)
        if kind in 'SR':
            size = struct.unpack('<I', self._read(4))[0]
            if self.handle.tell() + size > limit:
                raise ValueError('The FBX property leaves its own record')
            if kind == 'R' or size > FBX_MAX_STRING:
                # Embedded content, or a string no dependency name can be
                # this long: the bytes are skipped, never copied.
                self.handle.seek(size, os.SEEK_CUR)
                return _FbxProperty(kind, None)
            return _FbxProperty(kind, self._read(size).decode('utf-8', 'replace'))
        raise ValueError(f'Unknown FBX property type {kind!r}')

    def _skip_array(self, kind: str, limit: int):
        width = {'f': 4, 'd': 8, 'l': 8, 'i': 4, 'b': 1}[kind]
        modern = self.modern_arrays
        if not modern and self.version >= 7000:
            start = self.handle.tell()
            header = self.handle.read(12)
            if len(header) == 12:
                n, encoding, stored = struct.unpack('<III', header)
                remaining = limit - start - 12
                modern = (encoding == 0 and stored == n * width and stored <= remaining
                          or encoding == 1 and 0 < stored <= remaining
                          and self.handle.read(1) == b'\x78')
            self.handle.seek(start)
        if modern:
            length, encoding, stored = struct.unpack('<III', self._read(12))
            size = int(stored) if encoding else int(stored or length * width)
        else:
            length = struct.unpack('<I', self._read(4))[0]
            size = int(length) * width
        if size < 0 or self.handle.tell() + size > limit:
            raise ValueError('The FBX array leaves its own record')
        self.handle.seek(size, os.SEEK_CUR)
        return _FbxProperty(kind, None)


class _FbxNode:
    """One record of an FBX container."""

    __slots__ = ('name', 'properties', 'children', 'end')

    def __init__(self, name, properties, children, end):
        self.name = name
        self.properties = properties
        self.children = children
        self.end = end

    @property
    def base(self):
        return self.name.split('::')[0]


def _collect_fbx_node(node, references: list) -> None:
    """Collect the texture references of one record and of its children."""
    if node.base in ('Texture', 'Video', 'Media'):
        _collect_fbx_object(node, references)
    for child in node.children:
        _collect_fbx_node(child, references)


def _collect_fbx_object(node, references: list) -> None:
    """One Texture/Video/Media record states at most one file.

    FBX declares the path in two different shapes depending on the writer:
    as ``Properties70`` entries (older 7.x files) or as direct child records
    named ``RelativeFilename``/``FileName`` (the shape 7.4 writes).  Both are
    read; neither is guessed from the object's own name.
    """
    if _has_embedded_content(node):
        references.append(DetectedReference(
            reference='', role=ROLE_TEXTURE, resolution=RES_EMBEDDED,
            detail='Media.Content'))
        return
    values = _fbx_path_properties(node)
    relative = values.get('RelativeFilename') or ''
    absolute = values.get('FileName') or values.get('Reference') or ''
    reference = relative or absolute
    if not reference:
        return
    # The property a writer chose is not proof of the shape of its value: an
    # FBX written on Windows puts an absolute path in ``RelativeFilename``.
    # The value itself decides.
    is_absolute = _is_absolute_reference(reference)
    references.append(DetectedReference(
        reference=reference, role=ROLE_TEXTURE,
        resolution=RES_ABSOLUTE if is_absolute else RES_RELATIVE,
        detail='RelativeFilename' if relative else 'FileName'))


def _has_embedded_content(node) -> bool:
    """True when the record carries its own bytes instead of a file name.

    Only this record and its own ``Content`` child count: a ``Texture`` that
    points at a ``Media`` record is external even when that record is not.
    """
    for prop in node.properties:
        if prop.kind == 'R':
            return True
    for child in node.children:
        if child.base == 'Content':
            for prop in child.properties:
                if prop.kind == 'R':
                    return True
    return False


def _fbx_path_properties(node) -> dict:
    """The path properties an object declares, keyed by property name."""
    values = {}
    for child in node.children:
        name = child.base
        if name in FBX_PATH_KEYS and child.properties:
            value = child.properties[0].value
            if isinstance(value, str) and value and name not in values:
                values[name] = value
            continue
        if name != 'Properties70':
            continue
        for entry in child.children:
            if entry.base != 'P' or len(entry.properties) < 2:
                continue
            key = entry.properties[0].value
            if not isinstance(key, str) or key not in FBX_PATH_KEYS:
                continue
            value = entry.properties[-1].value
            if isinstance(value, str) and value and key not in values:
                values[key] = value
    return values


def _parse_ascii_fbx(path: Path) -> tuple:
    references = []
    current = None
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith(';'):
            continue
        if line.startswith(('Texture:', 'Video:', 'Media:')):
            current = line.split(':', 1)[0]
            continue
        if line.startswith('P:') and current in ('Texture', 'Video'):
            parts = _ascii_property(line)
            if parts is None:
                continue
            key, value = parts
            if key not in FBX_PATH_KEYS or not value:
                continue
            references.append(DetectedReference(
                reference=value, role=ROLE_TEXTURE,
                resolution=RES_ABSOLUTE if _is_absolute_reference(value)
                else RES_RELATIVE, detail=key))
        elif line == '}':
            current = None
    return tuple(_one_reference_per_object(references))


def _ascii_property(line: str):
    """The name and value of an ASCII ``P:`` property line.

    ``P: "RelativeFilename", "string", "", "", "textures/a.png"`` -- the value
    is the last field, quoted, and it may itself contain commas.
    """
    body = line[2:].strip()
    if not body.startswith('"'):
        return None
    end = body.find('"', 1)
    if end < 0:
        return None
    key = body[1:end]
    rest = body[end + 1:].strip()
    fields = _ascii_fields(rest)
    if len(fields) < 2:
        return None
    return key, fields[-1].strip()


def _ascii_fields(text: str) -> list:
    """Split an ASCII FBX property list on unquoted commas."""
    fields, current, quoted = [], [], False
    for character in text:
        if character == '"':
            quoted = not quoted
            current.append(character)
            continue
        if character == ',' and not quoted:
            fields.append(''.join(current))
            current = []
            continue
        current.append(character)
    fields.append(''.join(current))
    cleaned = []
    for field in fields:
        value = field.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        cleaned.append(value)
    return cleaned


def _one_reference_per_object(references) -> list:
    """Keep one reference per file an FBX object states.

    FBX writers name the same texture twice (a relative and an absolute form,
    or a Texture and its Video).  The relative form is the relocatable one, so
    it wins; two different references are never merged.
    """
    by_target = {}
    order = []
    for reference in references:
        key = (reference.reference.lower(), reference.resolution)
        if key not in by_target:
            by_target[key] = reference
            order.append(key)
            continue
        existing = by_target[key]
        if existing.resolution == RES_ABSOLUTE and reference.resolution == RES_RELATIVE:
            by_target[key] = reference
    result = [by_target[key] for key in order]
    # An absolute and a relative statement of the same file are one file.
    relative_names = {Path(item.reference).name.lower() for item in result
                      if item.resolution == RES_RELATIVE and item.reference}
    return [item for item in result
            if not (item.resolution == RES_ABSOLUTE
                    and Path(item.reference).name.lower() in relative_names)]


# -- path helpers ----------------------------------------------------------


def _is_absolute_reference(reference: str) -> bool:
    if not reference:
        return False
    if reference.startswith(('/', '\\')):
        return True
    return len(reference) > 1 and reference[1] == ':'


def _resolve_relative(base: Path, reference: str) -> Optional[str]:
    if not reference:
        return None
    candidate = Path(base).parent / reference
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return str(resolved) if resolved.is_file() else None


def _resolve_reference(base: Path, reference: str) -> Optional[str]:
    if not reference:
        return None
    if _is_absolute_reference(reference):
        candidate = Path(reference)
    else:
        candidate = Path(base).parent / reference
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return str(resolved) if resolved.is_file() else None


def detect_references(path: Path) -> tuple:
    """Every dependency the model file declares, per format."""
    suffix = Path(path).suffix.lower()
    if suffix == '.obj':
        references = list(parse_obj_references(Path(path)))
        for reference in list(references):
            if reference.role == ROLE_MATERIAL and reference.path:
                references.extend(parse_mtl_references(Path(reference.path)))
        return tuple(references)
    if suffix == '.gltf':
        return parse_gltf_references(Path(path))
    if suffix == '.glb':
        return parse_glb_references(Path(path))
    if suffix == '.fbx':
        return parse_fbx_references(Path(path))
    raise ModelStorageError(f'Unsupported model format: {suffix}')


# ---------------------------------------------------------------------------
# package layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedFile:
    """One file that will be copied into the project."""

    source: str
    package_path: str
    role: str
    reference: str
    absolute_reference: bool
    origin: str
    missing: bool = False
    size_bytes: int = 0
    sha256: str = ''


def plan_package(main_path: Path, references: tuple) -> tuple:
    """Lay the model out inside the project, keeping relative relations.

    The package root is the deepest directory that holds the model and every
    dependency that was found.  A dependency that lives on another volume
    cannot keep its relation; it is stored under ``_external/<id>/`` and the
    original absolute reference is recorded.
    """
    main = Path(main_path).resolve()
    found = [main]
    for reference in references:
        if reference.path:
            candidate = Path(reference.path).resolve()
            if candidate not in found:
                found.append(candidate)
    root = _common_directory(found) if found else main.parent
    planned = [PlannedFile(source=str(main),
                            package_path=_relative(main, root),
                            role=ROLE_MAIN, reference=main.name,
                            absolute_reference=False, origin='imported')]
    seen = {str(main)}
    for reference in references:
        if reference.path:
            source = str(Path(reference.path).resolve())
            if source in seen:
                continue
            seen.add(source)
            package_path = _relative(Path(source), root)
            if package_path is None:
                package_path = _external_path(Path(source))
            planned.append(PlannedFile(
                source=source, package_path=package_path, role=reference.role,
                reference=reference.reference,
                absolute_reference=reference.resolution == RES_ABSOLUTE,
                origin=reference.detail))
        else:
            if reference.resolution == RES_EMBEDDED:
                continue
            planned.append(PlannedFile(
                source='', package_path='', role=reference.role,
                reference=reference.reference,
                absolute_reference=reference.resolution == RES_ABSOLUTE,
                origin=reference.detail, missing=True))
    return tuple(planned)


def _common_directory(paths) -> Path:
    directories = [Path(path).resolve().parent for path in paths]
    if not directories:
        return Path(paths[0]).resolve().parent
    parts = [directory.parts for directory in directories]
    common = []
    for group in zip(*parts):
        if len(set(group)) == 1:
            common.append(group[0])
        else:
            break
    if not common:
        return directories[0]
    return Path(*common)


def _relative(path: Path, root: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _external_path(path: Path) -> str:
    token = hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]
    return f'_external/{token}/{path.name}'


# ---------------------------------------------------------------------------
# the importer
# ---------------------------------------------------------------------------


class ModelImporter:
    """Import models into one project, in the background."""

    def __init__(self, project_context: ModelProjectContext,
                 repository: ModelRepositoryAdapter,
                 publish_event=None):
        self.context = project_context
        self.repository = repository
        self._publish = publish_event or (lambda event: None)
        self._lock = threading.RLock()
        self._jobs = {}
        self._cancelled = set()

    # -- public API ------------------------------------------------------

    def import_paths(self, paths, *, folder_id: Optional[str] = None, metadata=None) -> str:
        """Accept an import job.  The returned id is **not** a completion."""
        accepted = tuple(str(path) for path in paths)
        if not accepted:
            raise ValueError('没有选择要导入的模型文件')
        for path in accepted:
            source = Path(path)
            if not source.is_file():
                raise ValueError(f'模型文件不存在：{path}')
            if source.suffix.lower() not in MODEL_EXTENSIONS:
                raise ValueError(f'不支持的模型格式：{source.suffix}')
        job_id = uuid.uuid4().hex
        metadata = json.loads(json.dumps(metadata or {}))
        if set(metadata) - {'name','tags','notes','rating','deleted_at','eagle'}:
            raise ValueError('Unsupported import metadata')
        with self._lock:
            self._jobs[job_id] = {
                'paths': accepted,
                'folder_id': folder_id,
                'metadata': metadata,
                'state': 'queued',
                'cancel': threading.Event(),
                'thread': None,
                'session': self.context.session_token,
            }
        thread = threading.Thread(target=self._run, args=(job_id,),
                                  name=f'model-import-{job_id[:8]}', daemon=True)
        with self._lock:
            self._jobs[job_id]['thread'] = thread
        thread.start()
        return job_id

    def cancel(self, job_id: str) -> None:
        """Ask a job to stop.  A cancelled job leaves no ready asset."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job['cancel'].set()

    def close_project(self, project_session: str) -> None:
        """Cancel and join every job of one project session."""
        with self._lock:
            jobs = [(job_id, job) for job_id, job in self._jobs.items()
                    if job.get('session') == project_session]
        for _job_id, job in jobs:
            job['cancel'].set()
        for _job_id, job in jobs:
            thread = job.get('thread')
            if thread is not None and thread.is_alive():
                thread.join(timeout=30)

    def job_state(self, job_id: str) -> Optional[str]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job['state'] if job else None

    # -- worker ----------------------------------------------------------

    def _emit(self, event) -> None:
        try:
            self._publish(event)
        except Exception:  # pragma: no cover - a broken listener must not
            pass        # turn a committed import into a failure

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job['state'] = 'running'
            paths = job['paths']
            folder_id = job['folder_id']
        total = len(paths)
        for index, source in enumerate(paths):
            if job['cancel'].is_set():
                self._finish(job_id, 'cancelled')
                return
            self._emit(ModelJobPhaseEvent(
                job_id=job_id, job_kind='import', state='running',
                completed=index, total=total,
                message=f'正在读取 {Path(source).name}'))
            try:
                options = {'metadata': job['metadata']} if job['metadata'] else {}
                result = self._import_one(job_id, source, folder_id, job['cancel'], **options)
            except (_Cancelled, ModelOperationCancelled):
                self._finish(job_id, 'cancelled')
                return
            except ModelStorageError as error:
                self._emit(ModelImportFailedEvent(
                    job_id=job_id,
                    failure=ModelImportFailure(source=source,
                                               error_code='import_failed',
                                               message=str(error))))
                self._finish(job_id, 'failed')
                return
            except Exception as error:  # pragma: no cover - unexpected
                self._emit(ModelImportFailedEvent(
                    job_id=job_id,
                    failure=ModelImportFailure(source=source,
                                               error_code='unexpected_error',
                                               message=str(error))))
                self._finish(job_id, 'failed')
                return
            self._emit(ModelImportCompletedEvent(job_id=job_id, result=result))
        self._finish(job_id, 'completed')

    def _finish(self, job_id: str, state: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job['state'] = state
        self._emit(ModelJobPhaseEvent(job_id=job_id, job_kind='import', state=state))

    def _import_one(self, job_id: str, source: str, folder_id, cancel, *, metadata=None) -> ModelImportResult:
        # Each stage below is a fact the user can be stuck on: dependencies
        # found, bytes copied, hashes verified, record committed. They were all
        # in the code already; only the record of them was missing.
        started = time.perf_counter()
        main_path = Path(source).resolve()
        references = detect_references(main_path)
        planned = plan_package(main_path, references)
        missing = [item for item in planned if item.missing]
        absolute = [item for item in planned if getattr(item, 'absolute_reference', '')]
        record_model_event(EVENT_IMPORT_DEPENDENCIES_DETECTED, job_id=job_id,
                           source_name=main_path.name,
                           dependency_count=max(0, len(planned) - 1),
                           missing_count=len(missing),
                           absolute_count=len(absolute),
                           missing=[item.reference for item in missing][:8],
                           missing_truncated=len(missing) > 8,
                           duration_ms=round((time.perf_counter() - started) * 1e3, 2))
        staging = Path(tempfile.mkdtemp(prefix='prism-model-import-'))
        try:
            stage_started = time.perf_counter()
            staged = self._stage(planned, staging, cancel)
            usable = [item for item in staged if not item.missing]
            record_model_event(EVENT_IMPORT_COPIED, job_id=job_id,
                               source_name=main_path.name,
                               file_count=len(usable),
                               total_bytes=sum(item.size_bytes or 0 for item in usable),
                               duration_ms=round((time.perf_counter() - stage_started) * 1e3, 2))
            record_model_event(EVENT_IMPORT_VALIDATED, job_id=job_id,
                               source_name=main_path.name,
                               file_count=len(usable),
                               digest_verified=True,
                               missing_count=len(staged) - len(usable),
                               duration_ms=round((time.perf_counter() - stage_started) * 1e3, 2))
            if cancel.is_set():
                raise _Cancelled()
            commit_started = time.perf_counter()
            options = {'metadata': metadata} if metadata else {}
            _payload, result = self._commit(staged, staging, main_path, folder_id, cancel, **options)
            record_model_event(EVENT_IMPORT_COMMITTED, job_id=job_id,
                               model_id=getattr(result, 'model_id', ''),
                               source_name=main_path.name,
                               file_count=len(usable),
                               missing_count=len(staged) - len(usable),
                               duration_ms=round((time.perf_counter() - commit_started) * 1e3, 2),
                               total_duration_ms=round((time.perf_counter() - started) * 1e3, 2))
            return result
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _stage(self, planned: tuple, staging: Path, cancel) -> tuple:
        """Copy and verify every file before anything is committed."""
        staged = []
        for item in planned:
            if cancel.is_set():
                raise _Cancelled()
            if item.missing:
                staged.append(item)
                continue
            source = Path(item.source)
            before = source.stat()
            target = staging / item.package_path
            target.parent.mkdir(parents=True, exist_ok=True)
            self._copy_to_stage(source, target, cancel)
            after = source.stat()
            if (before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                # The source changed while it was copied: the copy is not the
                # file the user pointed at, so it is not stored.
                raise ModelStorageError(
                    f'导入过程中文件发生变化：{source}')
            digest, size = sha256_of(target)
            if size != after.st_size:
                raise ModelStorageError(f'复制后的文件大小不一致：{source}')
            staged.append(PlannedFile(
                source=item.source, package_path=item.package_path, role=item.role,
                reference=item.reference,
                absolute_reference=item.absolute_reference, origin=item.origin,
                missing=False, size_bytes=size, sha256=digest))
        return tuple(staged)

    def _copy_to_stage(self, source, target, cancel):
        """Bounded, cooperative copy; also the fault-injection boundary."""
        with source.open('rb') as origin, target.open('wb') as output:
            while True:
                if cancel.is_set():
                    raise _Cancelled()
                chunk = origin.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)

    def _commit(self, staged: tuple, staging: Path, main_path: Path, folder_id, cancel=None, *, metadata=None):
        """Write the originals and the record in one transaction."""
        usable = [item for item in staged if not item.missing]
        missing = [item for item in staged if item.missing]
        if not usable:
            raise ModelStorageError('没有可导入的文件')
        model_id = uuid.uuid4().hex
        file_ids = {item.package_path: uuid.uuid4().hex for item in usable}
        details = []
        payload_holder = {}
        main_item = next((item for item in usable if item.role == ROLE_MAIN),
                         usable[0])
        main_file_id = file_ids[main_item.package_path]

        def commit_row(connection, stored):
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            payload_files = []
            for item, resource in zip(usable, stored):
                detail = ModelFileDetail(
                    file_id=file_ids[item.package_path],
                    relative_path=item.package_path,
                    role=item.role,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                    source_ref=resource.source_ref,
                    origin=item.origin,
                    missing=False,
                    attachment_id=resource.attachment_id,
                    original_reference=item.reference,
                    absolute_reference=item.absolute_reference)
                details.append(detail)
                payload_files.append(detail.to_payload())
            for item in missing:
                detail = ModelFileDetail(
                    file_id=uuid.uuid4().hex,
                    relative_path=('_missing/' + hashlib.sha256(
                        item.reference.encode('utf-8')).hexdigest()[:16] + '/' +
                        item.reference.replace('\\', '/').rsplit('/', 1)[-1]), role=item.role,
                    size_bytes=0, sha256='', source_ref='',
                    origin=item.origin, missing=True,
                    original_reference=item.reference,
                    absolute_reference=item.absolute_reference)
                details.append(detail)
                payload_files.append(detail.to_payload())
            payload = self._build_payload(
                model_id, main_path, folder_id, main_file_id, payload_files, usable)
            payload.update(metadata or {})
            # The same rules the reader and the full save apply: no absolute
            # path, no "..", a managed reference for every present file.
            validate_model_records([payload], self.context.project_id)
            payload_holder['payload'] = self.repository.insert_in(connection, payload)

        stored = self.repository.resources.put_batch(
            [(staging / item.package_path, item.package_path.split('/')[-1])
             for item in usable],
            commit=commit_row, cancel=cancel.is_set if cancel is not None else None)
        payload = payload_holder['payload']
        result = ModelImportResult(
            model_id=model_id,
            name=main_path.name,
            format=payload['format'],
            payload=payload,
            files=tuple(details),
            missing=tuple(item.reference for item in missing),
            warnings=(),
            attachment_ids=tuple(detail.attachment_id for detail in details
                                 if detail.attachment_id is not None))
        return payload, result

    def _build_payload(self, model_id, main_path: Path, folder_id, main_file_id,
                       payload_files, usable) -> dict:
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
        return {
            'id': model_id,
            'model_id': model_id,
            'project_id': self.context.project_id,
            'name': main_path.name,
            'main_file_id': main_file_id,
            'format': main_path.suffix.lower().lstrip('.'),
            'source_revision': 1,
            'record_revision': 0,
            'folder_id': folder_id,
            'tags': [],
            'notes': '',
            'files': list(payload_files),
            'appearance': {'revision': 0, 'bindings': {}, 'properties': {}},
            'statistics': {'vertices': None, 'triangles': None, 'polygons': None,
                           'instances': None, 'basis': 'unknown', 'backend_version': ''},
            'unknown_fields': {},
            'created_at': stamp,
            'updated_at': stamp,
            'sort_order': 0,
            'total_size': sum(item.size_bytes for item in usable),
        }


class _Cancelled(Exception):
    """The job was cancelled before it committed anything."""
