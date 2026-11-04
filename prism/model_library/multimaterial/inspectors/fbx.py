"""FBX: objects, connections, per-primitive material assignment.

The reader in :mod:`prism.model_library.importer` only answers "which files
does this FBX name", so it skips every array property.  A material catalog
cannot: FBX stores *which polygon uses which material* in
``Geometry/LayerElementMaterial/Materials``, an integer array.  This module
therefore carries its own container reader that

* keeps integer/long/byte arrays (bounded), because per-polygon material
  indices are exactly that,
* refuses to load float arrays (vertex data) and reports only their length,
* reads embedded media only up to a bound, and says so when it did not,

and then reads the official object graph:

``Objects``
    ``Material``, ``Texture``, ``Video``, ``Geometry``, ``Model`` records.
``Connections``
    ``OO`` (object to object) and ``OP`` (object to property) links.  A
    material is assigned to a ``Model`` through ``OO``; a texture is attached to
    a material property through ``OP`` with the property name -- that name is
    the only honest source of "this is a diffuse map, not a bump map".

Nothing is guessed from object names, and a container that cannot be walked
raises :class:`CatalogParseError` instead of returning an empty catalog.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..catalog import (
    CHANNEL_R,
    CHANNEL_RGB,
    COLOR_LINEAR,
    COLOR_SRGB,
    CatalogBuilder,
    CatalogParseError,
    MaterialDefinition,
    ResourceRef,
    resource_id_of,
    texture_use,
)
from ..contracts import BindingSite
from . import locator as locators

PARSER_ID_FBX = 'fbx-7'

FBX_MAGIC = b'Kaydara FBX Binary  \x00'

#: Bounds.  A container that breaks its own promises stops the walk.
MAX_DEPTH = 64
MAX_RECORDS = 2_000_000
MAX_STRING_BYTES = 16 * 1024 * 1024
MAX_INT_ARRAY = 8_000_000

#: Material property names that map onto a slot:
#: ``property -> (slot, channels, colour space, note)``.
#:
#: FBX names are exporter-specific: a 3ds Max PBR export writes
#: ``3dsMax|main|base_color_map``, a Maya export writes
#: ``Maya|baseColor``.  The lookup therefore also tries the *last* segment of a
#: dotted/pipe-qualified name, and an unknown name is reported instead of being
#: dropped.  A role Prism cannot edit keeps its own slot and states why.
TEXTURE_SLOT = {
    'diffusecolor': ('basecolor', CHANNEL_RGB, COLOR_SRGB, ''),
    'diffuse': ('basecolor', CHANNEL_RGB, COLOR_SRGB, ''),
    'basecolor': ('basecolor', CHANNEL_RGB, COLOR_SRGB, ''),
    'base_color_map': ('basecolor', CHANNEL_RGB, COLOR_SRGB, ''),
    'diffuse_color': ('basecolor', CHANNEL_RGB, COLOR_SRGB, ''),
    'transparentcolor': ('opacity', CHANNEL_R, COLOR_LINEAR, ''),
    'opacity_map': ('opacity', CHANNEL_R, COLOR_LINEAR, ''),
    'opacity': ('opacity', CHANNEL_R, COLOR_LINEAR, ''),
    'normalmap': ('normal', CHANNEL_RGB, COLOR_LINEAR, ''),
    'norm_map': ('normal', CHANNEL_RGB, COLOR_LINEAR, ''),
    'normal_map': ('normal', CHANNEL_RGB, COLOR_LINEAR, ''),
    'emissivecolor': ('emissive', CHANNEL_RGB, COLOR_SRGB, ''),
    'emit_color_map': ('emissive', CHANNEL_RGB, COLOR_SRGB, ''),
    'emissive_map': ('emissive', CHANNEL_RGB, COLOR_SRGB, ''),
    'displacementcolor': ('displacement', CHANNEL_R, COLOR_LINEAR, ''),
    'displacement_map': ('displacement', CHANNEL_R, COLOR_LINEAR, ''),
    'metalness': ('metallic', CHANNEL_R, COLOR_LINEAR, ''),
    'metalness_map': ('metallic', CHANNEL_R, COLOR_LINEAR, ''),
    'metallic': ('metallic', CHANNEL_R, COLOR_LINEAR, ''),
    'roughness': ('roughness', CHANNEL_R, COLOR_LINEAR, ''),
    'roughness_map': ('roughness', CHANNEL_R, COLOR_LINEAR, ''),
    'ao_map': ('ao', CHANNEL_R, COLOR_LINEAR, ''),
    'occlusion': ('ao', CHANNEL_R, COLOR_LINEAR, ''),
    'occlusion_map': ('ao', CHANNEL_R, COLOR_LINEAR, ''),
    'bump': ('bump', CHANNEL_R, COLOR_LINEAR,
             'Bump 是高度贴图，不是法线贴图，未当作 normal'),
    'bump_map': ('bump', CHANNEL_R, COLOR_LINEAR,
                 'Bump 是高度贴图，不是法线贴图，未当作 normal'),
    'specularcolor': ('specular', CHANNEL_RGB, COLOR_SRGB,
                      'SpecularColor 是镜面贴图，不能当作粗糙度'),
    'specular_color_map': ('specular', CHANNEL_RGB, COLOR_SRGB,
                           'SpecularColorMap 是镜面贴图，不能当作粗糙度'),
    'specularfactor': ('specular', CHANNEL_R, COLOR_LINEAR,
                       'SpecularFactor 是镜面强度，不能当作粗糙度'),
    'shininessexponent': ('specular_exponent', CHANNEL_R, COLOR_LINEAR,
                          'ShininessExponent 是镜面指数，不能当作粗糙度'),
    'shininess': ('specular_exponent', CHANNEL_R, COLOR_LINEAR,
                  'Shininess 是镜面指数，不能当作粗糙度'),
    'reflectioncolor': ('reflection', CHANNEL_RGB, COLOR_SRGB,
                        'ReflectionColor 是反射贴图'),
    'reflection_map': ('reflection', CHANNEL_RGB, COLOR_SRGB,
                       'ReflectionMap 是反射贴图'),
    'reflectionfactor': ('reflection', CHANNEL_R, COLOR_LINEAR,
                         'ReflectionFactor 是反射强度贴图'),
    'ambientcolor': ('ambient', CHANNEL_RGB, COLOR_SRGB,
                     'AmbientColor 是环境色贴图，不是 AO'),
    'ambient_color_map': ('ambient', CHANNEL_RGB, COLOR_SRGB,
                          'AmbientColorMap 是环境色贴图，不是 AO'),
}

#: Properties70 names recorded with a Prism-facing name.
NUMERIC_PROPS = {
    'diffusecolor': 'basecolor_factor',
    'diffuse': 'basecolor_factor',
    'ambientcolor': 'ambient_factor_color',
    'specularcolor': 'specular_factor_color',
    'emissivecolor': 'emissive_factor_color',
    'emissive': 'emissive_factor_color',
    'opacity': 'opacity',
    'reflectivity': 'reflectivity',
    'shininess': 'shininess',
    'shininessexponent': 'specular_exponent',
}

#: Where a ``Video``/``Texture`` record states its file, in preference order.
PATH_CHILDREN = ('RelativeFilename', 'FileName', 'Filename')


@dataclass(frozen=True)
class FbxArray:
    """An array property.  ``values`` is ``None`` when it was not loaded."""

    kind: str
    count: int
    values: Optional[tuple] = None

    def __len__(self) -> int:
        return int(self.count)


@dataclass(frozen=True)
class FbxBlob:
    """A string/raw property too large to keep, or raw embedded content."""

    kind: str
    size: int
    data: Optional[bytes] = None


class FbxNode:
    """One record of a container."""

    __slots__ = ('name', 'properties', 'children')

    def __init__(self, name: str, properties: list, children: list):
        self.name = name
        self.properties = properties
        self.children = children

    @property
    def base(self) -> str:
        return self.name.split('::')[0]

    def child(self, name: str) -> Optional['FbxNode']:
        for item in self.children:
            if item.name == name:
                return item
        return None

    def child_base(self, name: str) -> Optional['FbxNode']:
        for item in self.children:
            if item.base == name:
                return item
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f'<FbxNode {self.name} props={len(self.properties)} children={len(self.children)}>'


def inspect_fbx(main_path, *, revision: int = 1):
    """Read one FBX container and freeze its material catalog."""
    path = Path(main_path)
    nodes, version = read_fbx(path)
    builder = CatalogBuilder(PARSER_ID_FBX, main_path=path)
    builder.note_unknown('fbx_version', version)
    _inspect(nodes, path, builder)
    return builder.build(revision=revision)


# ---------------------------------------------------------------------------
# container
# ---------------------------------------------------------------------------
def read_fbx(path: Path) -> tuple:
    """``(top level records, version)``.  Raises on an unreadable container."""
    path = Path(path)
    try:
        with path.open('rb') as handle:
            head = handle.read(27)
            if len(head) < 27:
                raise CatalogParseError('FBX 文件短于其头部')
            if not head.startswith(FBX_MAGIC):
                if head.startswith(FBX_MAGIC.strip(b'\x00')[:18]):
                    # The file claims to be a binary container but its magic is
                    # wrong.  Falling back to the ASCII reader would turn a
                    # damaged file into "a model with no materials".
                    raise CatalogParseError(
                        f'FBX 二进制容器头损坏：{head[:24]!r}')
                handle.seek(0)
                return (_read_ascii(handle.read().decode('utf-8', 'replace')), 'ascii')
            version = struct.unpack('<I', head[23:27])[0]
            nodes = _BinaryReader(handle, path.stat().st_size, version).read()
    except OSError as error:
        raise CatalogParseError(f'FBX 文件无法读取：{error}') from error
    if not nodes:
        raise CatalogParseError('FBX 容器里没有可读的记录')
    return nodes, str(version)


class _BinaryReader:
    """Walk the node tree of a binary FBX container, byte-bounded."""

    def __init__(self, handle, size: int, version: int):
        self.handle = handle
        self.size = int(size)
        self.version = int(version)
        self.wide = self.version >= 7500
        self.modern_arrays = self.version >= 7400
        self.header_size = 25 if self.wide else 13
        self.records = 0

    def read(self) -> list:
        self.handle.seek(27)
        nodes, _terminated = self._read_list(self.size, 0)
        return nodes

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
            if self.records > MAX_RECORDS:
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
            return None
        if start + name_length + property_length > self.size:
            return None
        name = self.handle.read(name_length).decode('utf-8', 'replace')
        properties_start = start + name_length
        self.handle.seek(properties_start)
        properties = self._read_properties(count, property_length)
        self.handle.seek(properties_start + property_length)
        children: list = []
        if depth < MAX_DEPTH:
            children, _terminated = self._read_list(end or self.size, depth + 1)
        if end:
            self.handle.seek(end)
        return FbxNode(name, properties, children)

    # -- properties ----------------------------------------------------
    def _read_properties(self, count: int, length: int) -> list:
        limit = self.handle.tell() + length
        values = []
        for _ in range(count):
            if self.handle.tell() >= limit:
                break
            try:
                value = self._read_property(limit)
            except (struct.error, ValueError, OSError, zlib.error):
                break
            values.append(value)
        return values

    def _read(self, size: int) -> bytes:
        data = self.handle.read(size)
        if len(data) != size:
            raise ValueError('FBX 容器被截断')
        return data

    def _read_property(self, limit: int):
        kind = self.handle.read(1)
        if not kind:
            raise ValueError('属性类型缺失')
        kind = kind.decode('ascii', 'replace')
        if kind == 'Y':
            return struct.unpack('<h', self._read(2))[0]
        if kind == 'C':
            return bool(self._read(1)[0])
        if kind == 'I':
            return struct.unpack('<i', self._read(4))[0]
        if kind == 'F':
            return struct.unpack('<f', self._read(4))[0]
        if kind == 'D':
            return struct.unpack('<d', self._read(8))[0]
        if kind == 'L':
            return struct.unpack('<q', self._read(8))[0]
        if kind in 'fdlib':
            return self._read_array(kind, limit)
        if kind in 'SR':
            size = struct.unpack('<I', self._read(4))[0]
            if self.handle.tell() + size > limit:
                raise ValueError('FBX 属性越过自身记录')
            if size > MAX_STRING_BYTES:
                self.handle.seek(size, os.SEEK_CUR)
                return FbxBlob(kind, size, None)
            data = self._read(size)
            if kind == 'R':
                return FbxBlob(kind, size, data)
            return data.decode('utf-8', 'replace')
        raise ValueError(f'未知的 FBX 属性类型 {kind!r}')

    def _read_array(self, kind: str, limit: int) -> FbxArray:
        width = {'f': 4, 'd': 8, 'l': 8, 'i': 4, 'b': 1}[kind]
        compressed = 0
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
            count, encoding, stored = struct.unpack('<III', self._read(12))
            size = int(stored) if encoding else int(stored or count * width)
            compressed = int(encoding)
        else:
            count = struct.unpack('<I', self._read(4))[0]
            size = int(count) * width
        if size < 0 or self.handle.tell() + size > limit:
            raise ValueError('FBX 数组越过自身记录')
        payload = self._read(size)
        # Geometry data is not needed and is never materialised; only the
        # small integer arrays that carry per-polygon material indices.
        if kind in 'fd':
            return FbxArray(kind, int(count), None)
        if int(count) > MAX_INT_ARRAY:
            return FbxArray(kind, int(count), None)
        if compressed == 1:
            expected = int(count) * width
            payload = zlib.decompress(payload, bufsize=expected)
        return FbxArray(kind, int(count), _unpack_array(kind, payload, int(count)))


def _unpack_array(kind: str, payload: bytes, count: int) -> tuple:
    format_code, width = {'i': ('<i', 4), 'b': ('<b', 1), 'l': ('<q', 8)}[kind]
    available = min(int(count), len(payload) // width)
    return struct.unpack('<' + format_code[-1] * available,
                         payload[:available * width])


# ---------------------------------------------------------------------------
# ASCII containers
# ---------------------------------------------------------------------------
def _read_ascii(text: str) -> list:
    """Parse an ASCII FBX container into the same node shape as the binary one."""
    tokens = _ascii_tokens(text)
    nodes, _index = _parse_ascii_records(tokens, 0, stop_at_brace=False)
    return nodes


def _ascii_tokens(text: str) -> list:
    """Tokens of an ASCII container, with line ends kept as separators.

    ASCII FBX ends a record at a line end (or a comma continuation), so the
    newline is a token instead of whitespace: without it a record would swallow
    the name of the next record as one of its own values.
    """
    tokens = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in '\r\n':
            tokens.append(('newline', ''))
            index += 1
            continue
        if char in ' \t':
            index += 1
            continue
        if char == ';':
            while index < length and text[index] not in '\r\n':
                index += 1
            continue
        if char == '"':
            index += 1
            buffer = []
            while index < length:
                if text[index] == '"':
                    if index + 1 < length and text[index + 1] == '"':
                        buffer.append('"')
                        index += 2
                        continue
                    index += 1
                    break
                buffer.append(text[index])
                index += 1
            tokens.append(('string', ''.join(buffer)))
            continue
        if char in ',:{}*':
            tokens.append(('symbol', char))
            index += 1
            continue
        start = index
        while index < length and text[index] not in ' \t\r\n,;:{}*"':
            index += 1
        tokens.append(('word', text[start:index]))
    return tokens


def _parse_ascii_records(tokens, index, *, stop_at_brace: bool):
    nodes = []
    while index < len(tokens):
        kind, value = tokens[index]
        if kind == 'newline':
            index += 1
            continue
        if kind == 'symbol' and value == '}':
            if stop_at_brace:
                return nodes, index + 1
            index += 1
            continue
        if kind != 'word':
            index += 1
            continue
        name = value
        index += 1
        if index < len(tokens) and tokens[index] == ('symbol', ':'):
            index += 1
        properties: list = []
        children: list = []
        ended_by_comma = False
        while index < len(tokens):
            kind, value = tokens[index]
            if kind == 'symbol' and value == '{':
                children, index = _parse_ascii_records(tokens, index + 1,
                                                      stop_at_brace=True)
                break
            if kind == 'newline':
                if ended_by_comma:
                    ended_by_comma = False
                    index += 1
                    continue
                index += 1
                break
            if kind == 'symbol' and value in ('}', ':'):
                break
            if kind == 'symbol' and value == '*':
                values, index = _parse_ascii_array(tokens, index + 1)
                properties.append(values)
                ended_by_comma = False
                continue
            if kind == 'symbol' and value == ',':
                ended_by_comma = True
                index += 1
                continue
            if kind == 'string':
                properties.append(value)
            elif kind == 'word':
                properties.append(_ascii_number(value))
            ended_by_comma = False
            index += 1
        nodes.append(FbxNode(name, properties, children))
    return nodes, index


def _parse_ascii_array(tokens, index):
    count = 0
    while index < len(tokens) and tokens[index][0] == 'newline':
        index += 1
    if index < len(tokens) and tokens[index][0] == 'word':
        try:
            count = int(tokens[index][1])
        except ValueError:
            count = 0
        index += 1
    values: list = []
    if index < len(tokens) and tokens[index] == ('symbol', '{'):
        index += 1
        while index < len(tokens) and tokens[index] != ('symbol', '}'):
            kind, value = tokens[index]
            if kind == 'newline' or (kind == 'symbol' and value == ','):
                index += 1
                continue
            if kind == 'word' and value.lower() == 'a':
                index += 1
                if index < len(tokens) and tokens[index] == ('symbol', ':'):
                    index += 1
                continue
            if kind == 'word':
                values.append(_ascii_number(value))
            index += 1
        index += 1
    kind = 'i'
    return FbxArray(kind, count or len(values), tuple(values)), index


def _ascii_number(value: str):
    text = str(value)
    try:
        if text.lower() in ('true', 'false'):
            return text.lower() == 'true'
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# ---------------------------------------------------------------------------
# the object graph
# ---------------------------------------------------------------------------
@dataclass
class _FbxScene:
    version: str = ''
    materials: dict = None
    textures: dict = None
    videos: dict = None
    geometries: dict = None
    models: dict = None
    links: list = None

    def __post_init__(self):
        self.materials = {}
        self.textures = {}
        self.videos = {}
        self.geometries = {}
        self.models = {}
        self.links = []


def _inspect(nodes, path: Path, builder: CatalogBuilder) -> None:
    scene = _collect(nodes, builder)
    base_dir = path.parent
    for uid, material in scene.materials.items():
        builder.add_definition(_definition(builder, scene, uid, material, base_dir))
    _add_bindings(builder, scene)


def _collect(nodes, builder: CatalogBuilder) -> _FbxScene:
    scene = _FbxScene()
    for node in nodes:
        if node.base == 'Objects':
            for obj in node.children:
                uid = _uid(obj)
                if uid is None:
                    continue
                if obj.base == 'Material':
                    scene.materials[uid] = obj
                elif obj.base == 'Texture':
                    scene.textures[uid] = obj
                elif obj.base in ('Video', 'Media'):
                    scene.videos[uid] = obj
                elif obj.base == 'Geometry':
                    scene.geometries[uid] = obj
                elif obj.base == 'Model':
                    scene.models[uid] = obj
                elif obj.base in ('LayeredTexture', 'ProceduralTexture'):
                    builder.note_unresolved(
                        f'{obj.base}（uid {uid}）是程序/分层贴图，未展开为具体图片')
        elif node.base == 'Connections':
            for relation in node.children:
                values = [item for item in relation.properties]
                if len(values) < 3:
                    continue
                scene.links.append(tuple(values[:4]))
    return scene


def _uid(obj: FbxNode) -> Optional[int]:
    if not obj.properties:
        return None
    value = obj.properties[0]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    return None


def _clean_name(obj: FbxNode) -> str:
    """The display name of an object, without its class marker.

    Binary 7.x writes ``name\\x00\\x01Class``; ASCII writes ``Class::name``.
    Both markers are stripped, and nothing else about the name is invented.
    """
    if len(obj.properties) < 2:
        return obj.name
    raw = obj.properties[1]
    if not isinstance(raw, str):
        return obj.name
    if '\x00\x01' in raw:
        raw = raw.split('\x00\x01')[0]
    elif '::' in raw:
        raw = raw.rsplit('::', 1)[-1]
    return raw.strip()


def _properties70(obj: FbxNode) -> dict:
    table = obj.child('Properties70')
    if table is None:
        return {}
    values = {}
    for entry in table.children:
        if entry.base not in ('P', 'Property'):
            continue
        if not entry.properties:
            continue
        name = str(entry.properties[0])
        values[name] = list(entry.properties[4:]) or list(entry.properties[1:])
    return values


def _shading_model(material: FbxNode) -> str:
    node = material.child('ShadingModel')
    if node is not None and node.properties and isinstance(node.properties[0], str):
        return node.properties[0].strip().lower()
    return ''


def _definition(builder: CatalogBuilder, scene: _FbxScene, uid: int,
                material: FbxNode, base_dir: Path) -> MaterialDefinition:
    properties70 = _properties70(material)
    properties = _numeric_properties(properties70)
    shading = _shading_model(material)
    unsupported: list = []
    textures = []
    for relation in scene.links:
        kind, child, parent, prop = _link(relation)
        if kind != 'OP' or parent != uid:
            continue
        texture = scene.textures.get(child)
        if texture is None:
            builder.note_unresolved(
                f'材质 {_clean_name(material)} 的属性 {prop} 指向不存在的贴图对象：{child}')
            continue
        mapping = _texture_slot(prop)
        if mapping is None:
            unsupported.append(f'未解释的贴图属性：{prop}（{_clean_name(texture)}）')
            continue
        slot, channels, color_space, note_text = mapping
        if note_text:
            unsupported.append(f'{note_text}（{_clean_name(texture)}）')
        resource, label, note, declared = _texture_resource(builder, scene, texture,
                                                           base_dir)
        if note:
            builder.note_unresolved(f'{_clean_name(material)} 的 {prop}：{note}')
        builder.note_resource_digest(resource.locator, resource.resource_id)
        provenance = _path_provenance(scene, texture, base_dir, resource, label)
        if provenance['candidate_conflict']:
            builder.note_unresolved('Texture path conflict: multiple existing candidates have different bytes')
        textures.append(texture_use(
            slot, resource, channels=channels, texcoord=_uv_set(texture),
            color_space=color_space,
            normal_convention='unspecified' if slot == 'normal' else '',
            source_locator=label,
            unknown={'fbx_property': prop, 'fbx_texture_uid': child, **provenance,
                     # Every (field, path) the file states is kept, not only the
                     # one that resolved: the absolute spelling is evidence
                     # about the author's machine, the relative one is what a
                     # move can still repair.
                     'declared_paths': [{'field': field, 'path': path}
                                        for field, path in declared],
                     'embedded': resource.embedded, 'missing': resource.missing,
                     'absolute': resource.absolute, 'remote': resource.remote}))
    name = _clean_name(material)
    if '\ufffd' in name:
        # The name's bytes are not valid UTF-8.  Identity is the object uid, so
        # the material stays the same material; only the display name is
        # damaged, and that is stated instead of being presented as complete.
        builder.note_unresolved(
            f'材质名含无法解码的字节（{name!r}）；身份仍是对象 uid {uid}，'
            '显示名需要人工确认')
    return MaterialDefinition(
        material_id=f'fbx:material:{uid}',
        source_key=str(uid),
        name=name,
        workflow=f'fbx_{shading}' if shading else 'fbx_unknown_shading',
        textures=tuple(textures),
        properties_json=json.dumps(properties, ensure_ascii=False, sort_keys=True),
        unsupported=tuple(dict.fromkeys(unsupported)),
        unknown_json=json.dumps({'properties70': properties70},
                                ensure_ascii=False, sort_keys=True),
    )


def _texture_slot(property_name) -> Optional[tuple]:
    """``(slot, channels, colour space, note)`` for one FBX property name.

    Exporter prefixes are stripped segment by segment, so
    ``3dsMax|main|base_color_map`` and ``Maya|baseColor`` both resolve without
    a table of every exporter's spelling.  ``None`` means the name is not
    understood at all, and the caller reports it.
    """
    text = str(property_name or '').strip()
    candidates = [text.lower()]
    for separator in ('|', '.'):
        if separator in text:
            candidates.append(text.rsplit(separator, 1)[-1].lower())
    compact = text.lower().replace(' ', '_')
    candidates.append(compact)
    for candidate in candidates:
        mapping = TEXTURE_SLOT.get(candidate)
        if mapping is not None:
            return mapping
        mapping = TEXTURE_SLOT.get(candidate.replace('-', '_'))
        if mapping is not None:
            return mapping
    return None


def _numeric_properties(properties70: dict) -> dict:
    properties: dict = {}
    for name, values in properties70.items():
        key = str(name).strip().lower()
        if key not in NUMERIC_PROPS:
            continue
        target = NUMERIC_PROPS[key]
        numbers = [float(value) for value in values
                   if isinstance(value, (int, float)) and not isinstance(value, bool)]
        properties[target] = numbers
    properties['present'] = sorted(properties70)
    return properties


def _texture_resource(builder: CatalogBuilder, scene: _FbxScene, texture: FbxNode,
                      base_dir: Path) -> tuple:
    """Resolve the file one ``Texture`` record actually names.

    A resolvable external file wins, because that is the file the user can see
    and replace.  Embedded content is used only when no external file can be
    found, and the resource id is then the hash of the decoded bytes.  A path
    that resolves to nothing is reported, never replaced by a guessed name.
    """
    texture_uid = _texture_uid(scene, texture)
    declarations: list = []
    linked = []
    for relation in scene.links:
        kind, child, parent, _prop = _link(relation)
        if kind != 'OO' or child != texture_uid or parent is None:
            continue
        video = scene.videos.get(parent)
        if video is not None:
            linked.append(video)
            declarations.extend(_video_declarations(video))
    declarations.extend(_video_declarations(texture))
    labels = [path for _field, path in declarations]
    attempted: list = []
    for label in labels:
        if label in attempted:
            continue
        attempted.append(label)
        resource, note = locators.read_reference(label, base_dir)
        if resource.resolved:
            return resource, label, note, tuple(declarations)
    inline = _inline_content(texture)
    if inline is None:
        for video in linked:
            inline = _inline_content(video)
            if inline is not None:
                break
    if inline is not None:
        data, size = inline
        locator_text = f'fbx:embedded:{size}'
        if data is None:
            return (ResourceRef(locator=locator_text, embedded=True, size_bytes=size),
                    locator_text,
                    f'内嵌媒体过大，未计算摘要（{size} 字节）', tuple(declarations))
        resource = ResourceRef(resource_id=resource_id_of(data),
                               locator=f'fbx:embedded:{len(data)}',
                               embedded=True, size_bytes=len(data))
        return resource, resource.locator, '', tuple(declarations)
    if labels:
        return (ResourceRef(locator=labels[0], missing=True), labels[0],
                f'找不到贴图文件：{labels[0]}', tuple(declarations))
    return (ResourceRef(locator=f'fbx:video-missing:{_clean_name(texture)}'),
            f'fbx:video-missing:{_clean_name(texture)}',
            f'贴图对象 {_clean_name(texture)} 没有文件路径', ())


def _path_provenance(scene, texture, base_dir, selected, selected_label):
    """Retain declarations independently of the resolver's adopted resource."""
    uid = _texture_uid(scene, texture)
    objects = []
    for relation in scene.links:
        kind, child, parent, _ = _link(relation)
        if kind == 'OO' and child == uid and parent in scene.videos:
            objects.append((parent, scene.videos[parent]))
    objects.append((uid, texture))
    declarations=[];candidates=[]
    for object_id, obj in objects:
        for field, value in _video_declarations(obj):
            declarations.append(dict(object_kind=obj.base, object_id=object_id,
                object_name=_clean_name(obj), field=field, value=value,
                absolute=locators.is_absolute_reference(value)))
            reference=locators.resolve_locator(value,base_dir)
            resource, _note=locators.read_reference(value,base_dir)
            candidates.append(dict(value=value,resolved=reference.path or '',
                exists=reference.path is not None,absolute=reference.absolute,
                digest=resource.resource_id.removeprefix('sha256:')))
    digests={candidate['digest'] for candidate in candidates if candidate['exists'] and candidate['digest']}
    chosen=selected.resource_id if selected.resolved else ''
    return dict(declarations=declarations,candidates=candidates,
        selected_resource_id=chosen,selected_path=selected_label if chosen else '',
        selection_reason=('First resolvable declared path in portable-first order' if chosen and not selected.embedded
                          else 'Embedded content' if chosen else 'No resolvable declared resource'),
        candidate_conflict=len(digests)>1)


def _texture_uid(scene: _FbxScene, texture: FbxNode) -> Optional[int]:
    for uid, item in scene.textures.items():
        if item is texture:
            return uid
    return None


def _video_declarations(video: FbxNode) -> list:
    """Every ``(field, path)`` one record states, most portable first.

    ``RelativeFilename`` comes before ``FileName``/``Filename`` because a
    relative path survives a project move; the absolute spelling is kept as a
    fallback **and** as evidence about the author's machine, so both are
    reported instead of only the one that happened to resolve.
    """
    declarations = []
    for name in PATH_CHILDREN:
        node = video.child(name)
        if node is not None and node.properties:
            value = node.properties[0]
            if isinstance(value, str) and value.strip():
                declarations.append((name, value.strip()))
    properties70 = _properties70(video)
    for key in ('Path', 'Filename'):
        for value in properties70.get(key) or ():
            if isinstance(value, str) and value.strip():
                declarations.append((f'Properties70.{key}', value.strip()))
    return declarations


def _inline_content(node: FbxNode) -> Optional[tuple]:
    """``(bytes or None, size)`` of an embedded ``Content`` property.

    ``Content`` lives either on the record itself or inside its ``Media``
    container.  A ``Media`` child that only names another object is a link, not
    content, so it is not mistaken for embedded bytes.
    """
    containers = [node]
    media = node.child('Media')
    if media is not None:
        containers.append(media)
    for container in containers:
        candidates = list(container.children)
        if container.child('Content') is None and container.properties:
            # ``Content`` may also be a plain record with one property.
            candidates = [container]
        for item in candidates:
            if item.base != 'Content' or not item.properties:
                continue
            value = item.properties[0]
            if isinstance(value, FbxBlob):
                if value.data is None:
                    return (None, value.size)
                data = _decode_blob(value.data)
                return (None, value.size) if data is None else (data, len(data))
            if isinstance(value, str):
                data = _decode_blob(value.encode('utf-8'))
                return (None, len(value)) if data is None else (data, len(data))
    return None


def _decode_blob(data: bytes) -> Optional[bytes]:
    """FBX 7 stores embedded media as base64; a raw blob is kept as it is.

    ``None`` means the value cannot be an image at all (a name, a link), so the
    caller reports it instead of hashing a file name as if it were content.
    """
    stripped = data.strip()
    if not stripped:
        return None
    try:
        return base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        pass
    if len(stripped) >= 8 and b'\x00' in stripped[:1024]:
        return data
    return None


def _uv_set(texture: FbxNode) -> int:
    """The UV set a texture names, as an index.

    FBX states it either as a ``Properties70`` entry or as a direct ``UVSet``
    record; both are read, and an unstated set means the first one.
    """
    candidates = list(_properties70(texture).get('UVSet') or [])
    node = texture.child('UVSet')
    if node is not None:
        candidates.extend(node.properties)
    for value in candidates:
        if isinstance(value, str) and value.strip():
            digits = ''.join(char for char in value if char.isdigit())
            if digits:
                return int(digits)
        if isinstance(value, int) and not isinstance(value, bool):
            return int(value)
    return 0


def _link(relation) -> tuple:
    values = list(relation) + [None] * (4 - len(relation))
    kind, child, parent, prop = values[:4]
    return (str(kind).strip(), _as_int(child), _as_int(parent),
            str(prop) if prop is not None else '')


def _as_int(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


# ---------------------------------------------------------------------------
# binding sites
# ---------------------------------------------------------------------------
def _add_bindings(builder: CatalogBuilder, scene: _FbxScene) -> None:
    material_of_model: dict = {}
    geometry_of_model: dict = {}
    model_parent: dict = {}
    material_of_geometry: dict = {}
    for relation in scene.links:
        kind, child, parent, _prop = _link(relation)
        if kind != 'OO' or child is None or parent is None:
            continue
        if child in scene.materials and parent in scene.models:
            material_of_model.setdefault(parent, []).append(child)
        elif child in scene.geometries and parent in scene.models:
            geometry_of_model.setdefault(parent, []).append(child)
        elif child in scene.materials and parent in scene.geometries:
            material_of_geometry.setdefault(parent, []).append(child)
        elif child in scene.models and parent in scene.models:
            model_parent[child] = parent
        elif child in scene.models and parent == 0:
            model_parent.setdefault(child, 0)

    face_counts: dict = {}
    for model_uid, model in scene.models.items():
        for geometry_uid in geometry_of_model.get(model_uid, ()):
            geometry = scene.geometries.get(geometry_uid)
            if geometry is None:
                continue
            materials = list(material_of_model.get(model_uid) or ())
            if not materials:
                materials = list(material_of_geometry.get(geometry_uid) or ())
            node_path = _model_path(model_uid, scene, model_parent)
            geometry_key = f'fbx:geometry:{geometry_uid}'
            indices, counts, note = _material_indices(geometry, len(materials))
            if note:
                builder.note_unresolved(f'{_clean_name(model)}（uid {model_uid}）：{note}')
            if not materials:
                binding_id = (f'fbx|model:{model_uid}|geometry:{geometry_uid}'
                              f'|material:none')
                builder.add_binding(BindingSite(
                    binding_id=binding_id,
                    source_key=f'geometry:{geometry_uid}',
                    node_path=node_path,
                    geometry_key=geometry_key,
                    primitive_key='material:none',
                    original_material_id=None))
                builder.note_binding_resolution(binding_id, 'unassigned')
                builder.note_unresolved(
                    f'{node_path or model_uid} 的几何没有连接任何材质（使用 FBX 默认材质）')
                continue
            for index in sorted(indices):
                binding_id = (f'fbx|model:{model_uid}|geometry:{geometry_uid}'
                              f'|material_index:{index}')
                if index >= len(materials):
                    # The layer names a slot the model never connected a material
                    # to.  The usage point is kept and marked dangling: dropping
                    # it would under-report where the surface expects a material.
                    builder.add_binding(BindingSite(
                        binding_id=binding_id,
                        source_key=f'geometry:{geometry_uid}|index:{index}',
                        node_path=node_path,
                        geometry_key=geometry_key,
                        primitive_key=f'fbx|layer:0|material_index:{index}',
                        original_material_id=None))
                    builder.note_binding_resolution(binding_id, 'dangling')
                    builder.note_unresolved(
                        f'{_clean_name(model)} 的 LayerElementMaterial 索引 {index} '
                        f'超出连接的材质数量 {len(materials)}；使用点保留，分配为悬空')
                    continue
                material_uid = materials[index]
                builder.add_binding(BindingSite(
                    binding_id=binding_id,
                    source_key=f'geometry:{geometry_uid}|index:{index}',
                    node_path=node_path,
                    geometry_key=geometry_key,
                    primitive_key=f'fbx|layer:0|material_index:{index}',
                    original_material_id=f'fbx:material:{material_uid}'))
                builder.note_binding_resolution(binding_id, 'assigned')
                if counts:
                    face_counts[binding_id] = counts.get(index, 0)
    if face_counts:
        builder.note_unknown('face_counts', face_counts)


def _material_indices(geometry: FbxNode, material_count: int) -> tuple:
    """``(indices, per-index counts, note)`` for one geometry layer."""
    layer = geometry.child('LayerElementMaterial')
    if layer is None:
        if material_count > 1:
            return ({0}, {}, '几何没有 LayerElementMaterial，'
                              '只记录了第一个连接的材质')
        return ({0}, {}, '')
    mapping = ''
    node = layer.child('MappingInformationType')
    if node is not None and node.properties:
        mapping = str(node.properties[0]).strip()
    materials = layer.child('Materials')
    values: tuple = ()
    if materials is not None and materials.properties:
        array = materials.properties[0]
        if isinstance(array, FbxArray) and array.values is not None:
            values = array.values
        elif isinstance(array, FbxArray):
            return ({0}, {}, 'LayerElementMaterial 的索引数组未被读取，'
                             '只记录了第一个材质')
    if mapping == 'AllSame' or not values:
        return ({int(values[0]) if values else 0}, {}, '')
    counts: dict = {}
    for value in values:
        index = int(value)
        counts[index] = counts.get(index, 0) + 1
    return (set(counts), counts, '')


def _model_path(model_uid: int, scene: _FbxScene, parents: dict) -> str:
    parts = []
    current = model_uid
    seen = set()
    while current is not None and current not in seen:
        seen.add(current)
        model = scene.models.get(current)
        if model is None:
            break
        parts.append(_clean_name(model) or f'model[{current}]')
        current = parents.get(current)
        if current == 0:
            break
    return '/'.join(reversed(parts))
