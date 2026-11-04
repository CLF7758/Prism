"""Independent reference readers for the four model formats.

These parsers read the **file bytes** and nothing else. They do not use the
Prism importer, the F3D backend or any third-party loader, because an
expectation that is produced by the same code that has to satisfy it is not
an expectation -- it is a copy of the answer.

Every parser reports the same facts:

* the file set the format refers to (the dependency closure)
* which of those references are missing on disk
* the geometry statistics, with the **basis** spelled out:
  ``file`` (what the file itself declares), ``loaded`` (what a loader would
  produce) and ``instances`` (how often a mesh is referenced).

The basis matters: a triangle count taken from a file is not the same number
as the triangle count a viewer shows, and the acceptance report has to say
which one it is talking about.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import urllib.parse
from dataclasses import dataclass, field

# ── generic helpers ───────────────────────────────────────────────────────
CHUNK = 1 << 20


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def git_blob_sha1(path):
    """The sha1 Git uses for a blob. Lets a download be proven byte-identical."""
    data = open(path, 'rb').read()
    header = b'blob %d\0' % len(data)
    return hashlib.sha1(header + data).hexdigest()


@dataclass
class ModelFacts:
    """What one file says about itself."""

    format: str
    ok: bool = True
    error: str | None = None
    # geometry
    vertices: int | None = None
    triangles: int | None = None
    polygons: int | None = None
    lines: int | None = None
    points: int | None = None
    meshes: int | None = None
    materials: int | None = None
    instances: int | None = None
    texture_count: int | None = None
    # features
    has_skin: bool | None = None
    has_animation: bool | None = None
    has_instancing: bool | None = None
    has_embedded_media: bool | None = None
    has_external_media: bool | None = None
    primitive_modes: tuple = ()
    uv_layers: int | None = None
    # dependencies
    dependencies: tuple = ()          # relative paths the file refers to
    missing: tuple = ()                # of those, the ones not on disk
    embedded: tuple = ()               # media stored inside the file
    absolute_references: tuple = ()    # another machine's paths; never to be used
    basis: str = 'file'
    extra: dict = field(default_factory=dict)

    def as_dict(self):
        data = {key: value for key, value in self.__dict__.items()}
        data['dependencies'] = list(self.dependencies)
        data['missing'] = list(self.missing)
        data['embedded'] = list(self.embedded)
        data['absolute_references'] = list(self.absolute_references)
        data['primitive_modes'] = list(self.primitive_modes)
        return data


def is_absolute_reference(reference):
    """True for a path that points at another machine, not at this asset."""
    return bool(re.match(r'^[A-Za-z]:[\\/]', reference)) or reference.startswith('\\\\')


def _resolve(base_dir, reference):
    """Resolve one referenced path against the file that referenced it."""
    reference = urllib.parse.unquote(reference.split('?')[0].split('#')[0].strip().strip('"'))
    if not reference:
        return None
    if re.match(r'^[A-Za-z]:[\\/]', reference) or reference.startswith('\\\\'):
        return os.path.normpath(reference)
    return os.path.normpath(os.path.join(base_dir, reference.replace('\\', '/')))


def _relative(base_dir, path):
    try:
        return os.path.relpath(path, base_dir).replace('\\', '/')
    except ValueError:
        return path


def _decode_text(data):
    """Decode a text container, honouring a UTF-16 BOM.

    A UTF-16 OBJ is still an OBJ. Reading it as UTF-8 with replacement
    characters would report "0 vertices, no materials" for a perfectly good
    file, which is exactly the kind of quiet wrong answer an expectation set
    exists to prevent.
    """
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16', errors='replace')
    if b'\x00' in data[:4]:
        # No byte-order mark but NUL-interleaved: UTF-16LE in practice.
        return data.decode('utf-16-le', errors='replace')
    return data.decode('utf-8', errors='replace')


def _read_text(path):
    with open(path, 'rb') as stream:
        return _decode_text(stream.read())


# ── OBJ / MTL ─────────────────────────────────────────────────────────────
_MTL_TEXTURE_KEYS = ('map_kd', 'map_ks', 'map_ke', 'map_ns', 'map_d', 'map_bump',
                     'map_disp', 'bump', 'displacement', 'map_rma', 'map_pr', 'norm',
                     'decal', 'refl', 'map_ka', 'map_kc', 'map_ke')
_MTL_FILE_KEYS = ('map_kd', 'map_ks', 'map_ke', 'map_ns', 'map_d', 'map_bump',
                  'map_disp', 'bump', 'displacement', 'map_rma', 'norm', 'decal',
                  'refl', 'map_ka', 'map_kc')


def parse_mtl(path):
    """The materials and the texture files one MTL refers to."""
    materials = []
    textures = []
    current = None
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ''
        if key == 'newmtl':
            current = value
            materials.append(value)
        elif key in _MTL_FILE_KEYS and value:
            textures.append(value.split()[0])
    return {'materials': materials, 'textures': textures}


def parse_obj(path):
    """The OBJ facts, and the MTL/texture closure it refers to."""
    base = os.path.dirname(os.path.abspath(path))
    facts = ModelFacts(format='obj')
    vertices = 0
    texcoords = 0
    normals = 0
    faces = []
    objects = []
    groups = []
    material_files = []
    text = _read_text(path)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        key = parts[0]
        if key == 'v':
            vertices += 1
        elif key == 'vt':
            texcoords += 1
        elif key == 'vn':
            normals += 1
        elif key == 'f':
            # A face is a polygon; the vertex count of the face decides
            # whether it is a triangle, a quad or an n-gon.
            count = 0
            for item in parts[1:]:
                count += 1
            if count:
                faces.append(count)
        elif key == 'o':
            objects.append(line[2:].strip())
        elif key == 'g':
            groups.append(line[2:].strip())
        elif key == 'mtllib':
            material_files.extend(line[len('mtllib'):].split())
    triangles = 0
    polygons = 0
    lines = 0
    points = 0
    for count in faces:
        if count == 1:
            points += 1
        elif count == 2:
            lines += 1
        else:
            polygons += 1
            triangles += count - 2
    facts.vertices = vertices
    facts.triangles = triangles
    facts.polygons = polygons
    facts.lines = lines
    facts.points = points
    facts.meshes = len(objects) or len(groups) or None
    facts.uv_layers = texcoords
    facts.extra.update({'objects': len(objects), 'groups': len(groups),
                        'normals': normals, 'face_count': len(faces),
                        'material_files': list(material_files)})
    dependencies = []
    materials = []
    textures = []
    for name in material_files:
        resolved = _resolve(base, name)
        if resolved is None:
            continue
        dependencies.append(_relative(base, resolved))
        if os.path.isfile(resolved):
            parsed = parse_mtl(resolved)
            materials.extend(parsed['materials'])
            mtl_dir = os.path.dirname(resolved)
            for texture in parsed['textures']:
                texture_path = _resolve(mtl_dir, texture)
                if texture_path is None:
                    continue
                dependencies.append(_relative(base, texture_path))
                textures.append(_relative(base, texture_path))
    facts.dependencies = tuple(sorted(set(dependencies)))
    facts.missing = tuple(sorted({item for item in dependencies
                                  if not os.path.isfile(_resolve(base, item))}))
    facts.materials = len(materials) or None
    facts.texture_count = len(textures) or None
    facts.has_external_media = bool(textures)
    facts.has_embedded_media = False
    facts.basis = 'file'
    return facts


# ── glTF / GLB ────────────────────────────────────────────────────────────
_GLTF_MODE = {0: 'points', 1: 'lines', 2: 'line_loop', 3: 'line_strip',
              4: 'triangles', 5: 'triangle_strip', 6: 'triangle_fan'}
_GLTF_COMPONENT_SIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_GLTF_TYPE_COMPONENTS = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4,
                         'MAT2': 4, 'MAT3': 9, 'MAT4': 16}


def _gltf_bytes_available(document, base_dir, bin_chunk_bytes):
    """How many bytes each declared buffer really has on disk (or in the GLB)."""
    available = []
    for buffer in document.get('buffers', []):
        uri = buffer.get('uri')
        if uri is None:
            available.append(bin_chunk_bytes)
        elif uri.startswith('data:'):
            import base64
            payload = uri.split(',', 1)[1] if ',' in uri else ''
            try:
                available.append(len(base64.b64decode(payload + '===')))
            except Exception:
                available.append(None)
        else:
            path = _resolve(base_dir, uri)
            available.append(os.path.getsize(path)
                             if path and os.path.isfile(path) else None)
    return available


def _gltf_integrity(document, available):
    """Structural damage a JSON-shaped reader can still prove.

    The point is *not* to validate every value: it is to refuse to call a file
    healthy when a declared byte range does not exist in the bytes we have.
    """
    problems = []
    for index, buffer in enumerate(document.get('buffers', [])):
        declared = buffer.get('byteLength')
        size = available[index] if index < len(available) else None
        if declared is not None and size is not None and size < declared:
            problems.append('buffer[%d] declares %d bytes but only %d are available'
                            % (index, declared, size))
    views = document.get('bufferViews', [])
    for index, view in enumerate(views):
        length = view.get('byteLength') or 0
        start = view.get('byteOffset') or 0
        buffer_index = view.get('buffer')
        size = available[buffer_index] if isinstance(buffer_index, int) \
            and 0 <= buffer_index < len(available) else None
        if size is not None and start + length > size:
            problems.append('bufferView[%d] spans %d..%d of a %d byte buffer[%s]'
                            % (index, start, start + length, size, buffer_index))
    for index, accessor in enumerate(document.get('accessors', [])):
        view_index = accessor.get('bufferView')
        if not isinstance(view_index, int) or not 0 <= view_index < len(views):
            continue
        view = views[view_index]
        elements = _GLTF_TYPE_COMPONENTS.get(accessor.get('type'), 1)
        component = _GLTF_COMPONENT_SIZE.get(accessor.get('componentType'), 0)
        count = accessor.get('count') or 0
        if not component:
            continue
        element_size = elements * component
        stride = view.get('byteStride') or element_size
        needed = (accessor.get('byteOffset') or 0)
        needed += max(0, count - 1) * stride + element_size
        length = view.get('byteLength') or 0
        if needed > length:
            problems.append('accessor[%d] needs %d bytes of bufferView[%d] (%d)'
                            % (index, needed, view_index, length))
    return problems


def _gltf_facts(document, base_dir, container, facts, bin_chunk_bytes=None):
    """The shared part of the glTF and the GLB reader."""
    buffers = document.get('buffers', [])
    images = document.get('images', [])
    materials = document.get('materials', [])
    meshes = document.get('meshes', [])
    accessors = document.get('accessors', [])
    nodes = document.get('nodes', [])
    skins = document.get('skins', [])
    animations = document.get('animations', [])
    dependencies = []
    embedded = []
    for buffer in buffers:
        uri = buffer.get('uri')
        if uri is None:
            embedded.append('buffer[%d]:embedded' % len(embedded))
        elif uri.startswith('data:'):
            embedded.append('buffer[%d]:data-uri' % len(embedded))
        else:
            dependencies.append(_relative(base_dir, _resolve(base_dir, uri)))
    for image in images:
        uri = image.get('uri')
        if uri is None:
            embedded.append('image[%d]:buffer-view' % len(embedded))
        elif uri.startswith('data:'):
            embedded.append('image[%d]:data-uri' % len(embedded))
        else:
            dependencies.append(_relative(base_dir, _resolve(base_dir, uri)))
    triangles = 0
    polygons = 0
    lines = 0
    points = 0
    modes = set()
    uv_layers = 0
    position_counts = []
    for mesh in meshes:
        for primitive in mesh.get('primitives', []):
            mode = primitive.get('mode', 4)
            modes.add(mode)
            attributes = primitive.get('attributes', {})
            uv_layers = max(uv_layers, sum(1 for key in attributes if key.startswith('TEXCOORD')))
            accessor = attributes.get('POSITION')
            count = None
            if accessor is not None and accessor < len(accessors):
                count = accessors[accessor].get('count')
                position_counts.append(count)
            index = primitive.get('indices')
            indices = accessors[index].get('count') if index is not None and index < len(accessors) else count
            if indices is None:
                continue
            if mode == 4:
                triangles += indices // 3
                polygons += indices // 3
            elif mode == 5:
                triangles += max(0, indices - 2)
                polygons += max(0, indices - 2)
            elif mode == 6:
                triangles += max(0, indices - 2)
                polygons += max(0, indices - 2)
            elif mode == 3:
                lines += max(0, indices - 1)
            elif mode in (1, 2):
                lines += indices // 2
            elif mode == 0:
                points += indices
    # Instances: a mesh that more than one node references.
    usage = {}
    for node in nodes:
        mesh = node.get('mesh')
        if mesh is not None:
            usage[mesh] = usage.get(mesh, 0) + 1
    instances = sum(count - 1 for count in usage.values() if count > 1)
    facts.vertices = max(position_counts) if position_counts else None
    facts.triangles = triangles
    facts.polygons = polygons
    facts.lines = lines
    facts.points = points
    facts.meshes = len(meshes) or None
    facts.materials = len(materials) or None
    facts.instances = instances
    facts.has_instancing = bool(instances) or None
    facts.has_skin = bool(skins) or None
    facts.has_animation = bool(animations) or None
    facts.primitive_modes = tuple(sorted(_GLTF_MODE.get(mode, str(mode)) for mode in modes))
    facts.uv_layers = uv_layers or None
    facts.dependencies = tuple(sorted(set(dependencies)))
    facts.missing = tuple(sorted({item for item in dependencies
                                  if not os.path.isfile(_resolve(base_dir, item))}))
    facts.embedded = tuple(embedded)
    facts.has_embedded_media = bool(embedded) or None
    facts.has_external_media = bool(dependencies) or None
    facts.extra.update({'generator': document.get('asset', {}).get('generator'),
                        'version': document.get('asset', {}).get('version'),
                        'node_count': len(nodes),
                        'accessor_count': len(accessors),
                        'container': container,
                        'texture_count': sum(len(material.get('textures', []) or [])
                                             for material in materials)})
    available = _gltf_bytes_available(document, base_dir, bin_chunk_bytes)
    problems = _gltf_integrity(document, available)
    facts.extra['buffer_bytes_available'] = available
    if problems:
        # The JSON parsed; the bytes it promises are not there. A model with a
        # truncated payload is damaged, not "readable with fewer fields".
        facts.extra['integrity_problems'] = problems
        facts.ok = False
        facts.error = 'structural damage: ' + '; '.join(problems[:4])
    return facts


def parse_gltf(path):
    base = os.path.dirname(os.path.abspath(path))
    facts = ModelFacts(format='gltf')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as stream:
            document = json.load(stream)
    except Exception as error:
        facts.ok = False
        facts.error = '%s: %s' % (type(error).__name__, error)
        return facts
    _gltf_facts(document, base, 'gltf', facts)
    return facts


def parse_glb(path):
    base = os.path.dirname(os.path.abspath(path))
    facts = ModelFacts(format='glb')
    try:
        with open(path, 'rb') as stream:
            header = stream.read(12)
            if len(header) < 12:
                raise ValueError('truncated GLB header')
            magic, version, length = struct.unpack('<4sII', header)
            if magic != b'glTF':
                raise ValueError('bad GLB magic %r' % magic)
            document = None
            binary = 0
            declared_length = length
            while True:
                chunk_header = stream.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_length, chunk_type = struct.unpack('<I4s', chunk_header)
                payload = stream.read(chunk_length)
                if len(payload) < chunk_length:
                    raise ValueError('GLB chunk %r is truncated: %d of %d bytes'
                                     % (chunk_type, len(payload), chunk_length))
                if chunk_type == b'JSON':
                    document = json.loads(payload.decode('utf-8').rstrip('\x00 '))
                elif chunk_type == b'BIN\x00':
                    binary = chunk_length
            if document is None:
                raise ValueError('GLB has no JSON chunk')
            actual_length = os.path.getsize(path)
            if declared_length != actual_length:
                raise ValueError('GLB header declares %d bytes, file has %d'
                                 % (declared_length, actual_length))
    except Exception as error:
        facts.ok = False
        facts.error = '%s: %s' % (type(error).__name__, error)
        return facts
    facts.extra['bin_chunk_bytes'] = binary
    facts.extra['glb_version'] = version
    _gltf_facts(document, base, 'glb', facts, bin_chunk_bytes=binary)
    return facts


# ── FBX ───────────────────────────────────────────────────────────────────
#: A binary FBX starts with 21 bytes of magic (the tail byte is a real NUL),
#: then 0x1A 0x00, then the version. The whole container is little-endian.
_FBX_MAGIC_TEXT = b'Kaydara FBX Binary'
_FBX_BINARY_MAGIC = b'Kaydara FBX Binary  \x00'
_FBX_HEADER_BYTES = 27

#: Property type codes the binary container may carry.
_FBX_SCALARS = {
    b'C': ('<B', 1), b'Y': ('<h', 2), b'I': ('<i', 4),
    b'F': ('<f', 4), b'D': ('<d', 8), b'L': ('<q', 8),
}
_FBX_ARRAYS = {b'f': ('f', 4), b'd': ('d', 8), b'l': ('q', 8),
               b'i': ('i', 4), b'b': ('B', 1), b'c': ('B', 1)}


def _fbx_array_header(data, position, wide, item_size):
    """Read an array property header, refusing a layout that cannot be true.

    The FBX SDK widened the element count and the compressed length to 64 bit
    somewhere around version 7500, but writers in the wild are not consistent:
    ``boxWithCompressedCTypeArray.FBX`` advertises version 7500 and still uses
    32 bit counters. Both layouts are therefore tried and the one that is
    internally consistent is kept -- guessing silently is how a reader starts
    inventing geometry.
    """
    records = ['<QIQ', '<III'] if wide else ['<III']
    for record in records:
        header_bytes = struct.calcsize(record)
        if position + header_bytes > len(data):
            continue
        length, encoding, compressed = struct.unpack(
            record, data[position:position + header_bytes])
        if encoding not in (0, 1):
            continue
        if position + header_bytes + compressed > len(data):
            continue
        if encoding == 0 and compressed != length * item_size:
            continue
        return length, encoding, compressed, header_bytes
    raise ValueError('no consistent FBX array header at %d' % position)


def _fbx_binary_nodes(data):
    """Walk the binary FBX node tree and yield (name, properties, depth).

    The container is little-endian. Version 7500 widened the node header and
    the array length fields to 64 bit; both layouts are read here, because
    the samples use 7.3/7.4 *and* newer writers exist.
    """
    if not data.startswith(_FBX_BINARY_MAGIC):
        return
    if len(data) < _FBX_HEADER_BYTES:
        raise ValueError('truncated FBX header')
    version = struct.unpack('<I', data[23:27])[0]
    wide = version >= 7500
    header = 25 if wide else 13
    offset_size = 'Q' if wide else 'I'

    def read_property(position):
        kind = data[position:position + 1]
        if not kind:
            raise ValueError('truncated FBX property at %d' % position)
        position += 1
        if kind in _FBX_SCALARS:
            code, size = _FBX_SCALARS[kind]
            if position + size > len(data):
                raise ValueError('truncated FBX scalar at %d' % position)
            return struct.unpack(code, data[position:position + size])[0], position + size
        if kind in _FBX_ARRAYS:
            code, size = _FBX_ARRAYS[kind]
            length, encoding, compressed, header_bytes = _fbx_array_header(
                data, position, wide, size)
            position += header_bytes
            if position + compressed > len(data):
                raise ValueError('FBX array overruns the file: %d bytes at %d'
                                 % (compressed, position))
            payload = data[position:position + compressed]
            position += compressed
            if encoding == 1:
                import zlib
                payload = zlib.decompress(payload)
            if type(length) is not int or length < 0:
                raise ValueError('bad FBX array length %r' % (length,))
            usable = (len(payload) // size) * size
            if len(payload) < length * size:
                raise ValueError('FBX array declares %r elements but only %d bytes'
                                 % (length, len(payload)))
            return list(struct.unpack('<%d%s' % (length, code),
                                      payload[:length * size])), position
        if kind == b'S':
            # The string length stays 32 bit in every version.
            length = struct.unpack('<I', data[position:position + 4])[0]
            position += 4
            if position + length > len(data):
                raise ValueError('truncated FBX string at %d' % position)
            return data[position:position + length].decode('utf-8', 'replace'), position + length
        if kind == b'R':
            length = struct.unpack('<I', data[position:position + 4])[0]
            position += 4
            if position + length > len(data):
                raise ValueError('truncated FBX raw value at %d' % position)
            return data[position:position + length], position + length
        raise ValueError('unknown FBX property type %r at %d' % (kind, position - 1))

    def node(position):
        """Read one node record; ``None`` is the null record that ends a list."""
        if position + header >= len(data):
            return None
        if wide:
            end_offset, prop_count, prop_len = struct.unpack(
                '<QQQ', data[position:position + 24])
            name_len = data[position + 24]
        else:
            end_offset, prop_count, prop_len = struct.unpack(
                '<III', data[position:position + 12])
            name_len = data[position + 12]
        if not (end_offset or prop_count or prop_len or name_len):
            return None
        position += header
        if position + name_len > len(data):
            raise ValueError('truncated FBX node name at %d' % position)
        name = data[position:position + name_len].decode('utf-8', 'replace')
        position += name_len
        properties = []
        for _ in range(prop_count):
            value, position = read_property(position)
            properties.append(value)
        return name, properties, position, end_offset

    def walk(position, end, depth=0):
        while position < end:
            record = node(position)
            if record is None:
                return
            name, properties, after, end_offset = record
            yield name, properties, depth
            if end_offset > after and depth < 64:
                yield from walk(after, min(end_offset, end), depth + 1)
            next_position = end_offset if end_offset > after else after
            if next_position <= position:
                raise ValueError('FBX node tree does not advance at %d' % position)
            position = next_position

    yield from walk(_FBX_HEADER_BYTES, len(data))


def parse_fbx(path):
    """The FBX facts, for both the binary and the ASCII container."""
    base = os.path.dirname(os.path.abspath(path))
    facts = ModelFacts(format='fbx')
    try:
        with open(path, 'rb') as stream:
            data = stream.read()
    except Exception as error:
        facts.ok = False
        facts.error = '%s: %s' % (type(error).__name__, error)
        return facts
    dependencies = []
    absolute = []
    embedded = []
    vertices = None
    polygon_indices = None
    meshes = 0
    materials = 0
    textures = 0
    models = 0
    deformer_skins = 0
    animation = False
    uv_layers = 0
    container = 'unknown'
    geometries = []
    if data.startswith(_FBX_MAGIC_TEXT) and not data.startswith(_FBX_BINARY_MAGIC):
        # Bytes that look like the magic but are not: the container header was
        # damaged (a text transfer that turned NUL into something printable
        # produces exactly this). Say so instead of guessing.
        facts.ok = False
        facts.error = ('malformed binary header: %r != %r'
                       % (data[:len(_FBX_BINARY_MAGIC)], _FBX_BINARY_MAGIC))
        facts.extra.update({'container': 'malformed-binary'})
        return facts
    if data.startswith(_FBX_BINARY_MAGIC):
        container = 'binary'
        try:
            version = struct.unpack('<I', data[23:27])[0]
            facts.extra['fbx_version'] = version
            geometries = []
            current = None
            geometry_depth = None
            for name, properties, depth in _fbx_binary_nodes(data):
                if name == 'Geometry':
                    meshes += 1
                    current = {'vertices': None, 'indices': []}
                    geometries.append(current)
                    geometry_depth = depth
                    continue
                if current is not None and geometry_depth is not None \
                        and depth <= geometry_depth:
                    current = None
                    geometry_depth = None
                if name == 'Vertices':
                    if current is not None:
                        current['vertices'] = len(properties[0]) // 3 if properties else None
                    else:
                        vertices = len(properties[0]) // 3 if properties else None
                elif name == 'PolygonVertexIndex':
                    if current is not None:
                        current['indices'].append(properties[0] if properties else [])
                    else:
                        polygon_indices = properties[0] if properties else []
                elif name == 'Model':
                    models += 1
                elif name == 'Material':
                    materials += 1
                elif name == 'Texture':
                    textures += 1
                elif name == 'Deformer':
                    if any(isinstance(item, str) and item in ('Skin', 'Cluster')
                           for item in properties):
                        deformer_skins += 1
                elif name in ('AnimationCurve', 'AnimationCurveNode', 'AnimationStack'):
                    animation = True
                elif name == 'LayerElementUV':
                    uv_layers += 1
                elif name in ('RelativeFilename', 'Filename', 'FileName'):
                    if properties and isinstance(properties[0], str) and properties[0]:
                        value = properties[0]
                        if is_absolute_reference(value):
                            absolute.append(value)
                        else:
                            dependencies.append(value)
                elif name == 'Content':
                    embedded.append('media[%d]:embedded' % len(embedded))
                elif name == 'Properties70':
                    for item in properties:
                        if isinstance(item, str) and item.lower().endswith('.png'):
                            dependencies.append(item)
        except Exception as error:
            facts.ok = False
            facts.error = 'binary parse: %s: %s' % (type(error).__name__, error)
            return facts
    else:
        container = 'ascii'
        text = _decode_text(data)
        # ``Vertices: *3230 { a: ...`` -- the count between the colon and the
        # brace is optional in the wild, so it is matched optionally.
        match = re.search(r'Vertices:\s*\*?\d*\s*\{[^{}]*?\ba:\s*([^\r\n}]+)', text)
        if match:
            vertices = len([item for item in match.group(1).split(',') if item.strip()]) // 3
        match = re.search(r'PolygonVertexIndex:\s*\*?\d*\s*\{[^{}]*?\ba:\s*([^\r\n}]+)', text)
        if match:
            polygon_indices = [int(item) for item in match.group(1).split(',') if item.strip()]
        meshes = len(re.findall(r'\bGeometry:\s*\d+', text))
        materials = len(re.findall(r'\bMaterial:\s*\d+', text))
        textures = len(re.findall(r'\bTexture:\s*\d+', text))
        models = len(re.findall(r'\bModel:\s*\d+', text))
        deformer_skins = len(re.findall(r'Deformer:\s*\d+,\s*"[^"]*",\s*"Skin"', text))
        animation = bool(re.search(r'Animation(Curve|CurveNode|Stack)', text))
        uv_layers = len(re.findall(r'LayerElementUV:', text))
        for name in re.findall(r'\b(?:FileName|Filename|RelativeFilename):\s*"([^"]+)"', text):
            if not name or name.startswith('Content:'):
                continue
            if is_absolute_reference(name):
                absolute.append(name)
            else:
                dependencies.append(name)
        embedded.extend(['media:embedded'] * len(re.findall(r'\bContent:\s*"', text)))
    if geometries:
        vertices = sum(item['vertices'] or 0 for item in geometries) or None
        polygon_indices = [index for item in geometries
                           for chunk in item['indices'] for index in chunk]
        facts.extra['geometries'] = [
            {'vertices': item['vertices'],
             'index_count': sum(len(chunk) for chunk in item['indices'])}
            for item in geometries]
    triangles = None
    polygons = None
    if polygon_indices is not None:
        # A polygon ends with a negative index; the count is the run length.
        polygons = 0
        triangles = 0
        run = 0
        for index in polygon_indices:
            run += 1
            if index < 0:
                polygons += 1
                triangles += max(0, run - 2)
                run = 0
    facts.vertices = vertices
    facts.triangles = triangles
    facts.polygons = polygons
    facts.meshes = meshes or None
    facts.materials = materials or None
    facts.texture_count = textures or None
    facts.has_skin = bool(deformer_skins) or None
    facts.has_animation = animation or None
    facts.uv_layers = uv_layers or None
    facts.instances = max(0, models - meshes) if models and meshes else (models or None)
    facts.has_instancing = bool(models and meshes and models > meshes) or None
    resolved = []
    own = os.path.normcase(os.path.abspath(path))
    for reference in dependencies:
        path_value = _resolve(base, reference)
        if path_value is None:
            continue
        if os.path.normcase(os.path.abspath(path_value)) == own:
            continue                      # a file never depends on itself
        resolved.append(_relative(base, path_value))
    facts.dependencies = tuple(sorted(set(resolved)))
    facts.missing = tuple(sorted({item for item in resolved
                                  if not os.path.isfile(_resolve(base, item))}))
    facts.embedded = tuple(embedded)
    facts.absolute_references = tuple(sorted(set(absolute)))
    facts.has_embedded_media = bool(embedded) or None
    facts.has_external_media = bool(resolved) or None
    facts.extra.update({'container': container, 'model_count': models or None,
                        'deformer_skins': deformer_skins or None})
    facts.basis = 'file'
    return facts


# ── dispatcher ────────────────────────────────────────────────────────────
_PARSERS = {'.obj': parse_obj, '.mtl': parse_mtl, '.gltf': parse_gltf,
            '.glb': parse_glb, '.fbx': parse_fbx}


def read_facts(path):
    """Read one model file and report its facts."""
    extension = os.path.splitext(path)[1].lower()
    parser = _PARSERS.get(extension)
    if parser is None:
        return ModelFacts(format=extension.lstrip('.'), ok=False,
                          error='no reference reader for %s' % extension)
    if extension == '.mtl':
        parsed = parse_mtl(path)
        facts = ModelFacts(format='mtl')
        facts.materials = len(parsed['materials']) or None
        facts.texture_count = len(parsed['textures']) or None
        facts.dependencies = tuple(parsed['textures'])
        facts.basis = 'file'
        return facts
    return parser(path)


def dependency_closure(path):
    """The file set an importer has to collect for one model file.

    The main file plus every file it refers to, transitively, as relative
    paths. A missing file stays in the list: "the set that should exist" and
    "the set that is on disk" are two different answers.
    """
    facts = read_facts(path)
    base = os.path.dirname(os.path.abspath(path))
    closure = {os.path.basename(path)}
    pending = list(facts.dependencies)
    seen = set()
    while pending:
        reference = pending.pop()
        if reference in seen:
            continue
        seen.add(reference)
        closure.add(reference)
        resolved = _resolve(base, reference)
        if resolved and os.path.isfile(resolved) \
                and os.path.splitext(resolved)[1].lower() == '.mtl':
            parsed = parse_mtl(resolved)
            mtl_dir = os.path.dirname(resolved)
            for texture in parsed['textures']:
                pending.append(_relative(base, _resolve(mtl_dir, texture)))
    return closure, facts


__all__ = ['ModelFacts', 'read_facts', 'dependency_closure', 'parse_obj',
           'parse_mtl', 'parse_gltf', 'parse_glb', 'parse_fbx', 'sha256_file',
           'git_blob_sha1']
