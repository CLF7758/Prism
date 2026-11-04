"""Independent material-level reference readers for the four model formats.

Why this module exists
----------------------
An expectation that is produced by the code that has to satisfy it is not an
expectation, it is a copy of the answer.  So nothing here imports Prism, the
F3D backend, assimp, trimesh or any other loader: these readers walk the raw
file bytes (JSON text, OBJ/MTL text, GLB chunks, the FBX node tree) and report
only what the file itself declares.

What they report
----------------
Every reader produces the same :class:`MaterialFacts`:

* ``materials`` -- every *definition* the file declares, with its numeric
  values, texture uses, alpha mode, sidedness and extensions.
* ``sites`` -- every *binding site* (a node instance / geometry primitive or an
  OBJ group), including the ones that carry **no** material, because "no
  material assigned" and "material named X" are different facts.
* ``unused_materials`` -- definitions nothing binds to.
* ``dangling_material_references`` -- names a binding site uses that the file
  set never defines.  Counting those as "no material" hides a broken asset.
* ``dependencies`` / ``missing_dependencies`` / ``absolute_references`` -- the
  texture and buffer closure, which one path may not silently guess.

Basis discipline
----------------
Numbers that depend on interpretation are accompanied by the basis they were
taken on.  ``basis='file'`` means "the file says so"; a per-polygon FBX
material split is ``basis='file:layer-element-material'``.  A reader that could
not decide records ``sureness='low'`` plus a note instead of inventing a value.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import urllib.parse
from dataclasses import dataclass, field

CHUNK = 1 << 20

#: Resource ids are content digests, never file names.
DIGEST_PREFIX = 'sha256:'

# ── small helpers ─────────────────────────────────────────────────────────
def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def is_absolute_reference(reference):
    """True for a path that points at another machine, not at this asset."""
    if not reference:
        return False
    text = str(reference)
    return bool(re.match(r'^[A-Za-z]:[\\/]', text)) or text.startswith('\\\\')


def _resolve(base_dir, reference):
    """Resolve one reference against the file that referenced it."""
    if not reference:
        return None
    reference = urllib.parse.unquote(
        str(reference).split('?')[0].split('#')[0].strip().strip('"').strip("'"))
    if not reference:
        return None
    if is_absolute_reference(reference):
        return os.path.normpath(reference)
    return os.path.normpath(os.path.join(base_dir, reference.replace('\\', '/')))


def _relative(base_dir, path):
    try:
        return os.path.relpath(path, base_dir).replace('\\', '/')
    except ValueError:
        return path


def _decode_text(data):
    """Decode a text container, honouring a UTF-16 BOM.

    A UTF-16 OBJ is still an OBJ.  Reading it as UTF-8 with replacement would
    quietly rename every material, which is exactly the defect these fixtures
    are meant to catch.
    """
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16', 'replace')
    return data.decode('utf-8-sig', 'replace')


def _read_text(path):
    with open(path, 'rb') as stream:
        return _decode_text(stream.read())


def _read_raw_lines(path):
    """Yield ``(raw_line_bytes, display_text)`` for a text container.

    The bytes are what the file actually says; the text is what a human would
    read.  Material identity is built from the bytes, because ``b'Wood\xff'``
    and ``b'Wood\xfe'`` both *display* as ``Wood\ufffd`` while being two
    different materials.
    """
    with open(path, 'rb') as stream:
        data = stream.read()
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        # A UTF-16 container is certain: the decoded text is the identity.
        text = data.decode('utf-16', 'replace')
        for line in text.splitlines():
            yield line.encode('utf-8', 'replace'), line
        return
    for raw in data.splitlines():
        yield raw, raw.decode('utf-8-sig', 'replace')


def _name_digest(raw_name):
    """A short, stable identity for one raw name field."""
    if not raw_name:
        return ''
    return hashlib.sha256(bytes(raw_name)).hexdigest()[:16]


def _raw_named_values(path, keyword):
    """Every ``<keyword> <value>`` line, in file order, as (digest, hex)."""
    wanted = keyword.encode('ascii')
    values = []
    for raw, _text in _read_raw_lines(path):
        stripped = raw.lstrip()
        if not stripped:
            continue
        head, _, tail = stripped.partition(b' ')
        if head.lower() != wanted:
            head2, _, tail2 = stripped.partition(b'\t')
            if head2.lower() != wanted:
                continue
            tail = tail2
        value = tail.strip()
        if not value:
            continue
        hex_text = value.hex() if any(byte > 0x7E or byte < 0x20 for byte in value) else ''
        values.append((_name_digest(value), hex_text))
    return values


# ── records ───────────────────────────────────────────────────────────────
@dataclass
class TextureUse:
    """One texture slot on one material definition."""

    slot: str
    reference: str = ''
    resolved: str = ''
    exists: object = None                 # True / False / None (not knowable)
    storage: str = 'external'             # external | embedded | data-uri
    absolute: bool = False
    texcoord: object = None
    uv_transform: dict = field(default_factory=dict)
    color_space: str = 'unknown'
    channels: str = 'unknown'
    normal_convention: str = 'unknown'
    extra: dict = field(default_factory=dict)
    #: 每一处**声明**：来自哪个对象、哪个字段、写了什么。FBX 会给出两条
    #: （Texture 的 RelativeFilename 与 Video 的 FileName），glTF/MTL 通常一条。
    declarations: list = field(default_factory=list)
    #: 每一处声明的**解析尝试**：解析到哪个路径、在不在、为什么没被采用。
    candidates: list = field(default_factory=list)
    #: 真正**采用**的资源。没找到资源时必须为空 —— 首选字符串不等于采用的文件。
    selected_resource_id: str = ''
    selected_path: str = ''
    selection_reason: str = ''
    candidate_conflict: bool = False

    @property
    def resource_state(self):
        """resolved / missing / unknown -- one word the report can rely on."""
        if self.storage in ('embedded', 'data-uri'):
            return 'resolved'
        if self.exists is True:
            return 'resolved'
        if self.exists is False:
            return 'missing'
        return 'unknown'

    def as_dict(self):
        return {
            'slot': self.slot,
            'reference': self.reference,
            'resolved': self.resolved,
            'exists': self.exists,
            'storage': self.storage,
            'absolute': self.absolute,
            'texcoord': self.texcoord,
            'uv_transform': dict(self.uv_transform),
            'color_space': self.color_space,
            'channels': self.channels,
            'normal_convention': self.normal_convention,
            'extra': dict(self.extra),
            'resource_state': self.resource_state,
            'declarations': list(self.declarations),
            'candidates': list(self.candidates),
            'selected_resource_id': self.selected_resource_id,
            'selected_path': self.selected_path,
            'selection_reason': self.selection_reason,
            'candidate_conflict': self.candidate_conflict,
        }


@dataclass
class MaterialDefinition:
    index: int
    name: str
    locator: str
    workflow: str = 'unknown'
    properties: dict = field(default_factory=dict)
    textures: list = field(default_factory=list)
    alpha_mode: str = 'unknown'
    alpha_cutoff: object = None
    double_sided: object = None
    extensions: tuple = ()
    unsupported: tuple = ()
    #: 材质身份的真正依据：``newmtl`` 名称字段的**原始字节**摘要。显示名只用于
    #: 展示 —— 两个不同的字节序列可以解码成同一个字符串（替换字符碰撞），
    #: 所以身份不能建立在显示名上。
    name_bytes_sha256: str = ''
    name_bytes_hex: str = ''

    def as_dict(self):
        return {
            'index': self.index,
            'name': self.name,
            'name_bytes_sha256': self.name_bytes_sha256,
            'name_bytes_hex': self.name_bytes_hex,
            'locator': self.locator,
            'workflow': self.workflow,
            'properties': dict(self.properties),
            'textures': [item.as_dict() for item in self.textures],
            'alpha_mode': self.alpha_mode,
            'alpha_cutoff': self.alpha_cutoff,
            'double_sided': self.double_sided,
            'extensions': list(self.extensions),
            'unsupported': list(self.unsupported),
        }


@dataclass
class BindingSite:
    """One place a material is (or is not) bound."""

    site: str
    node_path: str
    material_index: object                 # None => nothing bound at this site
    material_name: str = ''
    face_count: object = None
    primitive_index: object = None
    geometry_key: str = ''
    basis: str = 'file'
    dangling: bool = False

    def as_dict(self):
        return {
            'site': self.site,
            'node_path': self.node_path,
            'material_index': self.material_index,
            'material_name': self.material_name,
            'face_count': self.face_count,
            'primitive_index': self.primitive_index,
            'geometry_key': self.geometry_key,
            'basis': self.basis,
            'dangling': self.dangling,
        }


@dataclass
class MaterialFacts:
    format: str
    ok: bool = True
    error: object = None
    container: str = ''
    materials: list = field(default_factory=list)
    sites: list = field(default_factory=list)
    unused_materials: list = field(default_factory=list)
    dangling_material_references: list = field(default_factory=list)
    duplicate_material_names: list = field(default_factory=list)
    dependencies: tuple = ()
    missing_dependencies: tuple = ()
    absolute_references: tuple = ()
    embedded_media: tuple = ()
    uv_sets: object = None
    material_libraries: tuple = ()
    notes: list = field(default_factory=list)
    sureness: str = 'high'
    basis: str = 'file'
    sha256: str = ''
    #: 原始字节身份 → 定义序号。显示名相同但字节不同的材质必须分开。
    name_bytes_index: dict = field(default_factory=dict)
    #: 文件里出现过的 ``usemtl`` 名称字节摘要（OBJ 专用）。
    used_name_digests: list = field(default_factory=list)

    # -- derived views the expectation set uses ---------------------------
    def usage_counts(self):
        """How many distinct sites bind each material definition.

        Four chair legs that share one wood material are **one** material used
        at four sites -- that is the number the panel has to show.
        """
        counts = {index: 0 for index in range(len(self.materials))}
        for site in self.sites:
            if site.material_index is None:
                continue
            counts[site.material_index] = counts.get(site.material_index, 0) + 1
        return counts

    def unassigned_sites(self):
        """Sites that bind nothing *and* name nothing.

        A site that names a material the file set never defines is a broken
        reference, not "no material": the two lead to different user advice, so
        they are reported separately.
        """
        return [site.site for site in self.sites
                if site.material_index is None and not site.dangling]

    def dangling_sites(self):
        return [site.site for site in self.sites
                if site.material_index is None and site.dangling]

    def assignment_summary(self):
        """Four different "this part shows no material" situations.

        They lead to different advice, so they are never merged:

        * ``no_usemtl_statement`` -- the file never states a material at all;
        * ``sites_before_first_usemtl`` -- the file does state materials, but
          these faces come before the first one;
        * ``dangling_sites`` -- a name is stated that the file set never defines;
        * ``missing_material_libraries`` -- the ``mtllib`` it names is not there.
        """
        without = [site.site for site in self.sites
                   if site.material_index is None and not site.dangling]
        dangling = [site.site for site in self.sites
                    if site.material_index is None and site.dangling]
        missing_libraries = [
            name for name in self.material_libraries
            if any(name.replace('/', os.sep) in dep.replace('/', os.sep)
                   for dep in self.missing_dependencies)
            or any(name in note for note in self.notes if 'mtllib' in note)]
        states_material = bool(self.used_name_digests) if self.format == 'obj' else None
        return {
            'statement': ('usemtl' if self.format == 'obj' else self.format),
            'has_material_statement': states_material,
            'no_usemtl_statement': (states_material is False),
            'sites_without_material': without,
            'sites_before_first_usemtl': without if states_material else [],
            'dangling_sites': dangling,
            'missing_material_libraries': sorted(set(missing_libraries)),
            'duplicate_display_names': list(self.duplicate_material_names),
            'distinct_byte_identities': len(self.name_bytes_index),
        }

    def as_dict(self):
        counts = self.usage_counts()
        return {
            'format': self.format,
            'ok': self.ok,
            'error': self.error,
            'container': self.container,
            'sha256': self.sha256,
            'basis': self.basis,
            'sureness': self.sureness,
            'material_count': len(self.materials),
            'materials': [item.as_dict() for item in self.materials],
            'sites': [site.as_dict() for site in self.sites],
            'usage_counts': {str(key): value for key, value in counts.items()},
            'unassigned_sites': self.unassigned_sites(),
            'dangling_sites': self.dangling_sites(),
            'assignment': self.assignment_summary(),
            'unused_materials': list(self.unused_materials),
            'dangling_material_references': list(self.dangling_material_references),
            'duplicate_material_names': list(self.duplicate_material_names),
            'material_libraries': list(self.material_libraries),
            'dependencies': list(self.dependencies),
            'missing_dependencies': list(self.missing_dependencies),
            'absolute_references': list(self.absolute_references),
            'embedded_media': list(self.embedded_media),
            'uv_sets': self.uv_sets,
            'notes': list(self.notes),
        }


# ── glTF / GLB ────────────────────────────────────────────────────────────
#: Which GLB chunk type carries what.
_GLB_JSON_CHUNK = 0x4E4F534A
_GLB_BIN_CHUNK = 0x004E4942

#: glTF defines these texture slots on a material.  The tuple is
#: (slot, channels, color space) -- the channel layout is the spec's, not a
#: guess, and it is the reason a naive "load the image as the base colour"
#: implementation gets an ORM map wrong.
_GLTF_TEXTURE_SLOTS = {
    'baseColorTexture': ('base_color', 'rgba', 'srgb'),
    'metallicRoughnessTexture': ('metallic_roughness', 'gb', 'linear'),
    'normalTexture': ('normal', 'rgb', 'linear'),
    'occlusionTexture': ('occlusion', 'r', 'linear'),
    'emissiveTexture': ('emissive', 'rgb', 'srgb'),
    'diffuseTexture': ('diffuse', 'rgba', 'srgb'),
    'specularGlossinessTexture': ('specular_glossiness', 'rgba', 'srgb'),
    'clearcoatTexture': ('clearcoat', 'r', 'linear'),
    'clearcoatNormalTexture': ('clearcoat_normal', 'rgb', 'linear'),
    'sheenColorTexture': ('sheen_color', 'rgb', 'srgb'),
    'transmissionTexture': ('transmission', 'r', 'linear'),
    'thicknessTexture': ('thickness', 'g', 'linear'),
}

#: Material extensions whose presence changes the rendering contract.  They
#: are recorded by name so that a capability claim can be checked against the
#: file instead of against a UI label.
_GLTF_MATERIAL_EXTENSIONS = {
    'KHR_materials_transmission': 'transmission',
    'KHR_materials_volume': 'volume',
    'KHR_materials_ior': 'ior',
    'KHR_materials_unlit': 'unlit',
    'KHR_materials_emissive_strength': 'emissive_strength',
    'KHR_materials_clearcoat': 'clearcoat',
    'KHR_materials_sheen': 'sheen',
    'KHR_materials_specular': 'specular',
    'KHR_materials_iridescence': 'iridescence',
    'KHR_materials_anisotropy': 'anisotropy',
    'KHR_materials_dispersion': 'dispersion',
    'KHR_materials_pbrSpecularGlossiness': 'pbr_specular_glossiness',
    'KHR_texture_transform': 'texture_transform',
}


def _read_glb(raw, path):
    """Split a GLB container into (json document, BIN chunk, notes)."""
    notes = []
    if raw[:4] != b'glTF':
        raise ValueError('not a GLB container: magic %r' % (raw[:4],))
    if len(raw) < 12:
        raise ValueError('truncated GLB header')
    version, declared = struct.unpack('<II', raw[4:12])
    if declared != len(raw):
        notes.append('GLB 头声明长度 %d，实际 %d' % (declared, len(raw)))
    document = None
    bin_chunk = b''
    position = 12
    limit = min(len(raw), declared or len(raw))
    while position + 8 <= limit:
        chunk_length, chunk_type = struct.unpack('<II', raw[position:position + 8])
        position += 8
        payload = raw[position:position + chunk_length]
        if len(payload) < chunk_length:
            raise ValueError('GLB chunk 截断：声明 %d，实际 %d'
                             % (chunk_length, len(payload)))
        position += chunk_length
        if chunk_type == _GLB_JSON_CHUNK:
            document = json.loads(payload.decode('utf-8').rstrip('\x00 '))
        elif chunk_type == _GLB_BIN_CHUNK:
            bin_chunk = payload
    if document is None:
        raise ValueError('GLB 没有 JSON 块')
    return document, bin_chunk, version, notes


def _gltf_texture_use(material, key, textures, images, buffer_views,
                      base_dir, has_bin, document_extensions):
    slot, channels, color_space = _GLTF_TEXTURE_SLOTS[key]
    info = material.get(key) or {}
    use = TextureUse(slot=slot, channels=channels, color_space=color_space)
    index = info.get('index')
    use.texcoord = info.get('texCoord', 0)
    if slot == 'normal':
        # glTF normals are OpenGL convention by specification (+V/green up).
        use.normal_convention = 'gl'
        use.extra['scale'] = info.get('scale', 1.0)
    if slot == 'occlusion':
        use.extra['strength'] = info.get('strength', 1.0)
    transform = (info.get('extensions') or {}).get('KHR_texture_transform') or {}
    if transform:
        use.uv_transform = {
            'offset': list(transform.get('offset', [0.0, 0.0])),
            'scale': list(transform.get('scale', [1.0, 1.0])),
            'rotation': transform.get('rotation', 0.0),
            'texcoord': transform.get('texCoord'),
        }
        if 'texCoord' in transform:
            use.texcoord = transform['texCoord']
    if index is None:
        use.reference = ''
        use.exists = None
        use.storage = 'unresolved'
        return use
    if not isinstance(index, int) or index >= len(textures):
        use.reference = 'texture[%r]' % (index,)
        use.exists = False
        use.storage = 'dangling'
        use.extra['dangling'] = True
        return use
    texture = textures[index] or {}
    image_index = texture.get('source')
    use.extra['texture_index'] = index
    use.extra['image_index'] = image_index
    use.extra['sampler'] = texture.get('sampler')
    if image_index is None or image_index >= len(images):
        use.reference = 'image[%r]' % (image_index,)
        use.exists = False
        use.storage = 'dangling'
        use.extra['dangling'] = True
        return use
    image = images[image_index] or {}
    uri = image.get('uri')
    if 'bufferView' in image:
        use.storage = 'embedded'
        use.exists = True
        use.reference = 'bufferView[%s]%s' % (
            image['bufferView'],
            '#' + image['name'] if image.get('name') else '')
        view = buffer_views[image['bufferView']] if image['bufferView'] < len(buffer_views) else {}
        use.extra['buffer_view'] = image['bufferView']
        use.extra['mime_type'] = image.get('mimeType')
        use.extra['byte_length'] = view.get('byteLength')
        if not has_bin:
            use.exists = False
            use.extra['missing_bin'] = True
        return use
    use.reference = uri or ''
    if not uri:
        use.exists = False
        use.storage = 'dangling'
        return use
    if uri.startswith('data:'):
        use.storage = 'data-uri'
        use.exists = True
        use.extra['byte_length'] = len(uri)
        return use
    use.absolute = is_absolute_reference(urllib.parse.unquote(uri))
    resolved = _resolve(base_dir, uri)
    if use.absolute:
        use.resolved = urllib.parse.unquote(uri)
        use.exists = False
    else:
        use.resolved = _relative(base_dir, resolved) if resolved else ''
        use.exists = bool(resolved and os.path.isfile(resolved))
    use.storage = 'external'
    if use.absolute:
        use.extra['absolute_author_path'] = True
    return use


def parse_gltf_materials(path):
    """Material definitions and binding sites of a .gltf or .glb file."""
    extension = os.path.splitext(path)[1].lower()
    facts = MaterialFacts(format='glb' if extension == '.glb' else 'gltf',
                          container='glb' if extension == '.glb' else 'json')
    try:
        with open(path, 'rb') as stream:
            raw = stream.read()
        facts.sha256 = hashlib.sha256(raw).hexdigest()
    except Exception as error:                       # noqa: BLE001 - reported
        facts.ok = False
        facts.error = '%s: %s' % (type(error).__name__, error)
        return facts
    base_dir = os.path.dirname(os.path.abspath(path))
    has_bin = False
    try:
        if extension == '.glb':
            document, bin_chunk, _version, notes = _read_glb(raw, path)
            facts.notes.extend(notes)
            has_bin = bool(bin_chunk)
        else:
            document = json.loads(raw.decode('utf-8-sig'))
            has_bin = True                     # a .gltf refers to buffers by URI
    except Exception as error:                       # noqa: BLE001 - reported
        facts.ok = False
        facts.error = '%s: %s' % (type(error).__name__, error)
        return facts
    if not isinstance(document, dict):
        facts.ok = False
        facts.error = 'glTF 根节点不是对象'
        return facts
    meshes = document.get('meshes') or []
    accessors = document.get('accessors') or []
    textures = document.get('textures') or []
    images = document.get('images') or []
    buffer_views = document.get('bufferViews') or []
    buffers = document.get('buffers') or []
    document_extensions = set(document.get('extensionsUsed') or [])
    facts.uv_sets = None

    # buffers: an external .bin that is not on disk makes the file unusable
    dependencies = []
    missing = []
    absolute = []
    embedded = []
    for index, buffer in enumerate(buffers):
        uri = (buffer or {}).get('uri')
        if uri is None:
            continue
        if uri.startswith('data:'):
            embedded.append('buffer[%d]:data-uri' % index)
            continue
        resolved = _resolve(base_dir, uri)
        if resolved and is_absolute_reference(urllib.parse.unquote(uri)):
            absolute.append(urllib.parse.unquote(uri))
        if resolved and os.path.normcase(resolved) != os.path.normcase(os.path.abspath(path)):
            dependencies.append(_relative(base_dir, resolved))
            if not os.path.isfile(resolved):
                missing.append(_relative(base_dir, resolved))

    for index, material in enumerate(document.get('materials') or []):
        material = material or {}
        definition = MaterialDefinition(
            index=index,
            name=material.get('name') or 'material[%d]' % index,
            locator='gltf:material[%d]' % index,
            workflow='pbr_metallic_roughness',
        )
        pbr = material.get('pbrMetallicRoughness') or {}
        if 'baseColorFactor' in pbr:
            definition.properties['base_color_factor'] = list(pbr['baseColorFactor'])
        if 'metallicFactor' in pbr:
            definition.properties['metallic_factor'] = pbr['metallicFactor']
        if 'roughnessFactor' in pbr:
            definition.properties['roughness_factor'] = pbr['roughnessFactor']
        if 'emissiveFactor' in material:
            definition.properties['emissive_factor'] = list(material['emissiveFactor'])
        if 'doubleSided' in material:
            definition.double_sided = bool(material['doubleSided'])
        else:
            definition.double_sided = False     # glTF default
        definition.alpha_mode = material.get('alphaMode', 'OPAQUE')
        if definition.alpha_mode == 'MASK':
            definition.alpha_cutoff = material.get('alphaCutoff', 0.5)
        material_extensions = set((material.get('extensions') or {}))
        definition.extensions = tuple(sorted(material_extensions))
        definition.unsupported = tuple(
            sorted({_GLTF_MATERIAL_EXTENSIONS[name] for name in material_extensions
                    if name in _GLTF_MATERIAL_EXTENSIONS}))
        for key in _GLTF_TEXTURE_SLOTS:
            if key in pbr:
                definition.textures.append(_gltf_texture_use(
                    pbr, key, textures, images, buffer_views, base_dir,
                    has_bin, document_extensions))
            elif key in material:
                definition.textures.append(_gltf_texture_use(
                    material, key, textures, images, buffer_views, base_dir,
                    has_bin, document_extensions))
        # extension-carried texture slots and scalars, recorded verbatim
        for name in ('KHR_materials_transmission', 'KHR_materials_volume',
                     'KHR_materials_ior', 'KHR_materials_emissive_strength',
                     'KHR_materials_specular', 'KHR_materials_clearcoat',
                     'KHR_materials_sheen'):
            payload = (material.get('extensions') or {}).get(name)
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if isinstance(value, dict) and 'index' in value:
                        use = _gltf_texture_use(
                            payload, key, textures, images, buffer_views,
                            base_dir, has_bin, document_extensions)
                        use.slot = '%s.%s' % (_GLTF_MATERIAL_EXTENSIONS.get(name, name), key)
                        definition.textures.append(use)
                    else:
                        definition.properties['%s.%s' % (
                            _GLTF_MATERIAL_EXTENSIONS.get(name, name), key)] = value
        facts.materials.append(definition)

    # uv sets: every TEXCOORD_n attribute any primitive declares
    uv_names = set()
    for mesh in meshes:
        for primitive in (mesh or {}).get('primitives') or []:
            for attribute in ((primitive or {}).get('attributes') or {}):
                if attribute.startswith('TEXCOORD_'):
                    uv_names.add(int(attribute.split('_', 1)[1]))
    facts.uv_sets = len(uv_names)

    # binding sites: walk the scene graph, so an instanced mesh yields one
    # site per node instance -- a chair leg is not "primitive 0".
    nodes = document.get('nodes') or []
    scenes = document.get('scenes') or []
    scene_index = document.get('scene', 0)
    if scenes and 0 <= scene_index < len(scenes):
        roots = list((scenes[scene_index] or {}).get('nodes') or [])
    else:
        children = set()
        for node in nodes:
            children.update((node or {}).get('children') or [])
        roots = [index for index in range(len(nodes)) if index not in children]
        if not nodes:
            roots = []
    reachable_meshes = set()
    visited = set()

    def walk(node_index, prefix):
        if node_index in visited or node_index >= len(nodes):
            facts.notes.append('节点循环或越界：%r' % (node_index,))
            return
        visited.add(node_index)
        node = nodes[node_index] or {}
        name = node.get('name') or 'node[%d]' % node_index
        node_path = '%s/%s' % (prefix, name)
        mesh_index = node.get('mesh')
        if isinstance(mesh_index, int) and mesh_index < len(meshes):
            reachable_meshes.add(mesh_index)
            mesh = meshes[mesh_index] or {}
            mesh_name = mesh.get('name') or 'mesh[%d]' % mesh_index
            for primitive_index, primitive in enumerate(mesh.get('primitives') or []):
                primitive = primitive or {}
                material_index = primitive.get('material')
                if isinstance(material_index, int) and material_index >= len(facts.materials):
                    facts.notes.append('primitive 引用越界材质 %d' % material_index)
                    material_index = None
                facts.sites.append(BindingSite(
                    site='node:%s/primitive[%d]' % (node_path, primitive_index),
                    node_path=node_path,
                    material_index=material_index,
                    material_name=(facts.materials[material_index].name
                                   if isinstance(material_index, int)
                                   and material_index < len(facts.materials) else ''),
                    primitive_index=primitive_index,
                    geometry_key='mesh[%d]:%s' % (mesh_index, mesh_name),
                    basis='file:node-instance',
                ))
        for child in node.get('children') or []:
            walk(child, node_path)

    if not nodes and meshes:
        # A glTF without nodes is legal but unusual: report the primitives
        # directly rather than reporting "no material bindings".
        facts.notes.append('文件没有 nodes，按 meshes 直接列使用点')
        for mesh_index, mesh in enumerate(meshes):
            for primitive_index, primitive in enumerate((mesh or {}).get('primitives') or []):
                facts.sites.append(BindingSite(
                    site='mesh[%d]/primitive[%d]' % (mesh_index, primitive_index),
                    node_path='',
                    material_index=(primitive or {}).get('material'),
                    primitive_index=primitive_index,
                    geometry_key='mesh[%d]' % mesh_index,
                    basis='file:mesh-primitive',
                ))
    else:
        for root in roots:
            walk(root, '')
    unreachable = set(range(len(meshes))) - reachable_meshes
    if unreachable:
        facts.notes.append('未被任何节点引用的 mesh：%s'
                           % sorted(unreachable))
    # GPU instancing keeps the geometry in one node and repeats it inside the
    # renderer, so node-path enumeration cannot see the repetition.  Say so
    # instead of reporting one usage site as if it were the only one.
    gpu_instanced = [index for index, node in enumerate(nodes)
                     if 'EXT_mesh_gpu_instancing' in ((node or {}).get('extensions') or {})]
    if gpu_instanced:
        facts.notes.append(
            '节点 %s 使用 EXT_mesh_gpu_instancing：实例数在扩展里，'
            '按节点路径枚举的使用点数低于真实实例数' % gpu_instanced)
        facts.sureness = 'medium'
    if 'KHR_materials_variants' in document_extensions:
        facts.notes.append('文件使用 KHR_materials_variants：同一 primitive 可按变体换材质')
        facts.sureness = 'medium'

    used = {site.material_index for site in facts.sites
            if site.material_index is not None}
    facts.unused_materials = [index for index in range(len(facts.materials))
                              if index not in used]
    names = [material.name for material in facts.materials]
    facts.duplicate_material_names = sorted({name for name in names
                                             if names.count(name) > 1})
    for definition in facts.materials:
        for use in definition.textures:
            if use.storage == 'external' and use.resolved:
                dependencies.append(use.resolved)
                if not use.exists:
                    missing.append(use.resolved)
            if use.absolute and use.reference:
                absolute.append(urllib.parse.unquote(use.reference))
            if use.storage == 'embedded':
                embedded.append('%s:%s' % (definition.locator, use.reference))
            if use.extra.get('dangling'):
                facts.dangling_material_references.append(
                    '%s:%s->%s' % (definition.locator, use.slot, use.reference))
    facts.dependencies = tuple(sorted(set(dependencies)))
    facts.missing_dependencies = tuple(sorted(set(missing)))
    facts.absolute_references = tuple(sorted(set(absolute)))
    facts.embedded_media = tuple(sorted(set(embedded)))
    facts.basis = 'file:gltf-json'
    return facts


# ── OBJ / MTL ─────────────────────────────────────────────────────────────
#: MTL map statements -> (slot, channels, colour space, is_normal_map)
_MTL_MAPS = {
    'map_kd': ('diffuse', 'rgb', 'srgb', False),
    'map_ka': ('ambient', 'rgb', 'srgb', False),
    'map_ks': ('specular', 'rgb', 'srgb', False),
    'map_ke': ('emissive', 'rgb', 'srgb', False),
    'map_ns': ('specular_exponent', 'r', 'linear', False),
    'map_d': ('opacity', 'a-or-luminance', 'linear', False),
    'map_tr': ('opacity_inverse', 'a-or-luminance', 'linear', False),
    'map_bump': ('bump', 'rgb', 'linear', False),
    'bump': ('bump', 'rgb', 'linear', False),
    'norm': ('normal', 'rgb', 'linear', True),
    'map_kn': ('normal', 'rgb', 'linear', True),
    'disp': ('displacement', 'r', 'linear', False),
    'decal': ('decal', 'rgba', 'srgb', False),
    'refl': ('reflection', 'rgb', 'srgb', False),
}

#: Numeric MTL statements that carry a single scalar or a colour.
_MTL_SCALARS = {
    'kd': 'diffuse_color', 'ka': 'ambient_color', 'ks': 'specular_color',
    'ke': 'emissive_color', 'ns': 'specular_exponent', 'ni': 'optical_density',
    'd': 'dissolve', 'tr': 'transparency', 'illum': 'illumination_model',
    'tf': 'transmission_filter', 'sharpness': 'sharpness',
}

#: Map options that take one numeric argument.
_MTL_MAP_OPTIONS = {
    '-bm': 'bump_multiplier', '-boost': 'boost', '-texres': 'texture_resolution',
    '-mm': 'modifier_base', '-o': 'offset', '-s': 'scale', '-t': 'turbulence',
    '-clamp': 'clamp', '-blendu': 'blend_u', '-blendv': 'blend_v',
    '-imf': 'imfchan', '-cc': 'color_correction',
}


def _mtl_texture_statement(tokens):
    """Split a ``map_*`` statement into (options, filename)."""
    options = {}
    index = 1
    while index < len(tokens) and tokens[index].startswith('-'):
        flag = tokens[index].lower()
        values = []
        index += 1
        while index < len(tokens) and not tokens[index].startswith('-'):
            values.append(tokens[index])
            index += 1
            if flag not in ('-o', '-s', '-t'):
                break
        name = _MTL_MAP_OPTIONS.get(flag, flag)
        options[name] = values
    filename = ' '.join(tokens[index:]).strip()
    return options, filename


def parse_mtl_materials(mtl_path, base_dir, facts):
    """Add every ``newmtl`` of one .mtl file to ``facts``."""
    text = _read_text(mtl_path)
    first_index = len(facts.materials)
    current = None
    ordinal = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        tokens = line.split()
        key = tokens[0].lower()
        if key == 'newmtl':
            name = ' '.join(tokens[1:]).strip()
            current = MaterialDefinition(
                index=len(facts.materials),
                name=name,
                locator='mtl:%s:newmtl[%d]' % (_relative(base_dir, mtl_path), ordinal),
                workflow='phong_or_lambert',
            )
            if '\ufffd' in name:
                # The MTL has no encoding declaration and is not valid UTF-8.
                # Say so with the byte evidence: whichever reader picks a
                # locale codec will show a different name on another machine.
                facts.notes.append('材质名含非 UTF-8 字节，名称仅供参考：%r' % name)
                facts.sureness = 'medium'
            facts.materials.append(current)
            ordinal += 1
            continue
        if current is None:
            facts.notes.append('MTL 在 newmtl 之前的语句被忽略：%s' % line)
            continue
        if key in _MTL_MAP_OPTIONS:
            continue
        if key in _MTL_MAPS:
            slot, channels, color_space, is_normal = _MTL_MAPS[key]
            options, filename = _mtl_texture_statement(tokens)
            use = TextureUse(
                slot=slot, reference=filename, channels=channels,
                color_space=color_space,
                normal_convention='normal-map' if is_normal else 'height',
            )
            use.uv_transform = {name: values for name, values in options.items() if values}
            if slot == 'bump' and 'bump_multiplier' in options:
                use.extra['bump_multiplier'] = options['bump_multiplier']
            use.absolute = is_absolute_reference(filename)
            resolved = _resolve(base_dir, filename)
            if not filename:
                use.exists = False
                use.storage = 'dangling'
            elif use.absolute:
                use.resolved = urllib.parse.unquote(filename)
                use.exists = False
            else:
                use.resolved = _relative(base_dir, resolved) if resolved else ''
                use.exists = bool(resolved and os.path.isfile(resolved))
            current.textures.append(use)
            continue
        if key in _MTL_SCALARS:
            values = tokens[1:]
            parsed = []
            for value in values:
                try:
                    parsed.append(float(value))
                except ValueError:
                    parsed.append(value)
            if len(parsed) == 1:
                current.properties[_MTL_SCALARS[key]] = parsed[0]
            else:
                current.properties[_MTL_SCALARS[key]] = parsed
            continue
        current.properties.setdefault('unsupported_statements', []).append(line)
    names = [material.name for material in facts.materials]
    facts.duplicate_material_names = sorted({name for name in names
                                             if names.count(name) > 1})
    for material in facts.materials:
        for use in material.textures:
            if use.slot == 'normal':
                use.normal_convention = 'normal-map:convention-unknown'
            if use.storage == 'external' and use.resolved:
                if use.absolute:
                    facts.absolute_references = tuple(sorted(
                        set(facts.absolute_references) | {urllib.parse.unquote(use.reference)}))
                else:
                    facts.dependencies = tuple(sorted(set(facts.dependencies) | {use.resolved}))
                if not use.exists:
                    facts.missing_dependencies = tuple(sorted(
                        set(facts.missing_dependencies) | {use.resolved}))
    # 原始字节身份：与显示名无关。解析出的定义与 ``newmtl`` 行按出现顺序一一
    # 对应；数量不符时明确记下来，而不是硬配。
    raw_names = _raw_named_values(mtl_path, 'newmtl')
    new_definitions = facts.materials[first_index:]
    if raw_names and len(raw_names) == len(new_definitions):
        for definition, (digest, hex_text) in zip(new_definitions, raw_names):
            definition.name_bytes_sha256 = digest
            definition.name_bytes_hex = hex_text
            facts.name_bytes_index.setdefault(digest, []).append(definition.index)
    elif raw_names:
        facts.notes.append(
            '原始字节扫描到 %d 个 newmtl、解析出 %d 个定义，身份不按字节建立'
            % (len(raw_names), len(new_definitions)))
    facts.basis = 'file:mtl-text'
    return facts


def parse_obj_materials(path):
    """Material definitions (via .mtl) and per-group binding sites of an OBJ."""
    facts = MaterialFacts(format='obj', container='text')
    base_dir = os.path.dirname(os.path.abspath(path))
    try:
        facts.sha256 = sha256_file(path)
        text = _read_text(path)
    except Exception as error:                       # noqa: BLE001 - reported
        facts.ok = False
        facts.error = '%s: %s' % (type(error).__name__, error)
        return facts

    absent_materials = []          # materials used in the file but not defined
    libraries = []
    # One binding site per (object, group, material): a group that is split by
    # two ``usemtl`` blocks still has one site, and ten boxes that each say
    # ``usemtl Wood`` are ten sites, not one.
    sites = {}
    current_object = ''
    current_group = ''
    current_material = ''
    current_material_digest = ''
    uv_sets = set()

    def site_for():
        key = (current_object, current_group, current_material,
               current_material_digest)
        return sites.setdefault(key, {'faces': 0, 'lines': 0, 'points': 0})

    for line_number, (raw_bytes, line) in enumerate(_read_raw_lines(path), start=1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        tokens = line.split()
        key = tokens[0].lower()
        if key == 'mtllib':
            reference = ' '.join(tokens[1:]).strip()
            candidates = []
            whole = _resolve(base_dir, reference)
            if whole and os.path.isfile(whole):
                candidates = [whole]
            else:
                for token in tokens[1:]:
                    resolved = _resolve(base_dir, token)
                    if resolved:
                        candidates.append(resolved)
                if len(candidates) > 1:
                    facts.notes.append(
                        '第 %d 行 mtllib 含空格，按空白拆分尝试 %d 个文件'
                        % (line_number, len(candidates)))
            for candidate in candidates:
                libraries.append(_relative(base_dir, candidate))
                if not os.path.isfile(candidate):
                    facts.missing_dependencies = tuple(sorted(
                        set(facts.missing_dependencies) | {_relative(base_dir, candidate)}))
                    continue
                before = len(facts.materials)
                parse_mtl_materials(candidate, base_dir, facts)
                for material in facts.materials[before:]:
                    material.locator = 'mtl:%s:newmtl[%s]' % (
                        _relative(base_dir, candidate), material.name)
            continue
        if key == 'o':
            current_object = ' '.join(tokens[1:]) or 'object'
            current_group = ''
            continue
        if key == 'g':
            current_group = ' '.join(tokens[1:]) or 'group'
            continue
        if key == 'usemtl':
            current_material = ' '.join(tokens[1:]).strip()
            # 身份取原始字节：两个不同字节序列可能显示成同一个名字。
            current_material_digest = _name_digest(
                b' '.join(raw_bytes.split()[1:]).strip())
            facts.used_name_digests.append(current_material_digest)
            absent_materials.append(current_material)
            continue
        if key == 'f':
            site_for()['faces'] += 1
            for token in tokens[1:]:
                if '/' in token:
                    pieces = token.split('/')
                    if len(pieces) > 1 and pieces[1]:
                        uv_sets.add(0)
            continue
        if key == 'l':
            site_for()['lines'] += 1
            continue
        if key == 'p':
            site_for()['points'] += 1
            continue
        if key == 'vt':
            uv_sets.add(0)
            continue
    facts.uv_sets = len(uv_sets) or None

    by_name = {}
    for material in facts.materials:
        by_name.setdefault(material.name, []).append(material)
    by_digest = {}
    for material in facts.materials:
        if material.name_bytes_sha256:
            by_digest.setdefault(material.name_bytes_sha256, []).append(material)
    for (object_name, group_name, material_name, digest), counts in sites.items():
        definition = by_digest.get(digest) if digest else None
        if definition:
            material_index = definition[0].index
        else:
            definition = by_name.get(material_name)
            material_index = definition[0].index if definition else None
            if definition and digest:
                facts.notes.append(
                    '使用点 %r 的原始字节与任何定义都不等，退回按显示名绑定'
                    % (material_name or '(none)'))
        duplicate = len(definition) > 1 if definition else False
        node_path = '/'.join(item for item in (object_name, group_name) if item) or '(default)'
        facts.sites.append(BindingSite(
            site='obj:%s/%s#%s' % (object_name or '(default)',
                                   group_name or '(default)',
                                   material_name or '(none)'),
            node_path=node_path,
            material_index=material_index,
            material_name=material_name,
            face_count=counts['faces'],
            geometry_key='obj:%s/%s' % (object_name or '(default)',
                                        group_name or '(default)'),
            basis='file:obj-group',
            # A name that no .mtl defines is not "no material": it is a broken
            # reference, and the two must stay distinguishable.
            dangling=bool(material_name) and definition is None,
        ))
        if definition is None and material_name:
            facts.dangling_material_references.append(material_name)
        if duplicate:
            facts.notes.append(
                '使用点 %s 引用的材质名 %r 在 MTL 中定义了 %d 次，按定义顺序取第一份'
                % (node_path, material_name, len(definition)))
    # A face block before any usemtl binds nothing at all; that is a fact the
    # panel must show as "无材质", not as material number 0.
    used = {site.material_index for site in facts.sites
            if site.material_index is not None}
    facts.unused_materials = [material.index for material in facts.materials
                              if material.index not in used]
    facts.dangling_material_references = sorted(set(facts.dangling_material_references))
    facts.material_libraries = tuple(sorted(set(libraries)))
    facts.basis = 'file:obj-text'
    return facts


# ── FBX ───────────────────────────────────────────────────────────────────
_FBX_BINARY_MAGIC = b'Kaydara FBX Binary  \x00'
_FBX_MAGIC_TEXT = b'Kaydara FBX Binary'
_FBX_HEADER_BYTES = 27
_FBX_SCALARS = {b'C': ('<B', 1), b'Y': ('<h', 2), b'I': ('<i', 4),
                b'F': ('<f', 4), b'D': ('<d', 8), b'L': ('<q', 8)}
_FBX_ARRAYS = {b'f': ('f', 4), b'd': ('d', 8), b'l': ('q', 8),
               b'i': ('i', 4), b'b': ('B', 1), b'c': ('B', 1)}

#: FBX texture connection property -> (slot, channels, colour space).
#:
#: The property name is written by whichever exporter produced the file, and
#: the names differ per vendor: ``DiffuseColor`` (FBX SDK), ``Maya|baseColor``,
#: ``3dsMax|main|base_color_map``.  :func:`_fbx_slot_for` normalises to the
#: last path segment without a trailing ``_map`` before looking a name up, so
#: a new vendor prefix does not turn every texture into "unknown".
_FBX_TEXTURE_SLOTS = {
    'diffusecolor': ('diffuse', 'rgb', 'srgb'),
    'diffuse': ('diffuse', 'rgb', 'srgb'),
    'diffusecolor_map': ('diffuse', 'rgb', 'srgb'),
    'basecolor': ('diffuse', 'rgb', 'srgb'),
    'base_color': ('diffuse', 'rgb', 'srgb'),
    'albedo': ('diffuse', 'rgb', 'srgb'),
    'normalmap': ('normal', 'rgb', 'linear'),
    'normal': ('normal', 'rgb', 'linear'),
    'norm': ('normal', 'rgb', 'linear'),
    'normalcamera': ('normal', 'rgb', 'linear'),
    'bump': ('bump', 'rgb', 'linear'),
    'bumpmap': ('bump', 'rgb', 'linear'),
    'transparentcolor': ('opacity', 'a-or-luminance', 'linear'),
    'transparencyfactor': ('opacity', 'a-or-luminance', 'linear'),
    'opacity': ('opacity', 'a-or-luminance', 'linear'),
    'emissivecolor': ('emissive', 'rgb', 'srgb'),
    'emissivefactor': ('emissive', 'rgb', 'srgb'),
    'emissive': ('emissive', 'rgb', 'srgb'),
    'emit_color': ('emissive', 'rgb', 'srgb'),
    'emission': ('emissive', 'rgb', 'srgb'),
    'specularcolor': ('specular', 'rgb', 'srgb'),
    'specularfactor': ('specular', 'rgb', 'srgb'),
    'specular': ('specular', 'rgb', 'srgb'),
    'shininessexponent': ('specular_exponent', 'r', 'linear'),
    'reflectioncolor': ('reflection', 'rgb', 'srgb'),
    'reflectionfactor': ('reflection', 'r', 'linear'),
    'metallic': ('metallic', 'r', 'linear'),
    'metalness': ('metallic', 'r', 'linear'),
    'roughness': ('roughness', 'r', 'linear'),
    'ao': ('occlusion', 'r', 'linear'),
    'occlusion': ('occlusion', 'r', 'linear'),
    'ambientocclusion': ('occlusion', 'r', 'linear'),
    'tango': ('unknown', 'rgb', 'unknown'),
}


def _fbx_slot_for(property_name):
    """Map an exporter-specific texture property name onto a slot."""
    text = str(property_name or '').strip().lower()
    if '|' in text:
        text = text.rsplit('|', 1)[1]
    if text.endswith('_map'):
        text = text[:-4]
    if text in _FBX_TEXTURE_SLOTS:
        return _FBX_TEXTURE_SLOTS[text]
    flat = text.replace('_', '').replace(' ', '')
    if flat in _FBX_TEXTURE_SLOTS:
        return _FBX_TEXTURE_SLOTS[flat]
    for key, value in _FBX_TEXTURE_SLOTS.items():
        if flat.startswith(key):
            return value
    return ('unknown', 'unknown', 'unknown')


class FbxNode:
    __slots__ = ('name', 'properties', 'children')

    def __init__(self, name, properties, children):
        self.name = name
        self.properties = properties
        self.children = children

    def first(self, name):
        for child in self.children:
            if child.name == name:
                return child
        return None

    def all(self, name):
        return [child for child in self.children if child.name == name]

    def __repr__(self):
        return 'FbxNode(%r, props=%d, children=%d)' % (
            self.name, len(self.properties), len(self.children))


def _fbx_array_header(data, position, wide, item_size):
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


def _fbx_tree(data):
    """Parse a binary FBX into a node tree (version 7.x, 32/64 bit headers)."""
    if not data.startswith(_FBX_BINARY_MAGIC):
        raise ValueError('not a binary FBX: %r' % (data[:len(_FBX_BINARY_MAGIC)],))
    version = struct.unpack('<I', data[23:27])[0]
    wide = version >= 7500
    header = 25 if wide else 13

    def read_property(position):
        kind = data[position:position + 1]
        if not kind:
            raise ValueError('truncated FBX property at %d' % position)
        position += 1
        if kind in _FBX_SCALARS:
            code, size = _FBX_SCALARS[kind]
            return struct.unpack(code, data[position:position + size])[0], position + size
        if kind in _FBX_ARRAYS:
            code, size = _FBX_ARRAYS[kind]
            length, encoding, compressed, header_bytes = _fbx_array_header(
                data, position, wide, size)
            position += header_bytes
            payload = data[position:position + compressed]
            position += compressed
            if encoding == 1:
                import zlib
                payload = zlib.decompress(payload)
            if len(payload) < length * size:
                raise ValueError('FBX array declares %d elements but only %d bytes'
                                 % (length, len(payload)))
            return list(struct.unpack('<%d%s' % (length, code),
                                      payload[:length * size])), position
        if kind == b'S':
            length = struct.unpack('<I', data[position:position + 4])[0]
            position += 4
            return data[position:position + length].decode('utf-8', 'replace'), position + length
        if kind == b'R':
            length = struct.unpack('<I', data[position:position + 4])[0]
            position += 4
            return data[position:position + length], position + length
        raise ValueError('unknown FBX property type %r at %d' % (kind, position - 1))

    def read_node(position, end):
        if position + header > len(data) or position >= end:
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
            return None                        # null record: end of this list
        cursor = position + header
        name = data[cursor:cursor + name_len].decode('utf-8', 'replace')
        cursor += name_len
        properties = []
        for _ in range(prop_count):
            value, cursor = read_property(cursor)
            properties.append(value)
        children = []
        if end_offset > cursor:
            children = read_list(cursor, min(end_offset, end))
        return FbxNode(name, properties, children), end_offset

    def read_list(position, end):
        items = []
        guard = 0
        while position < end and guard < 1_000_000:
            guard += 1
            record = read_node(position, end)
            if record is None:
                break
            node, end_offset = record
            items.append(node)
            next_position = end_offset if end_offset > position else position + header
            if next_position <= position:
                raise ValueError('FBX node tree does not advance at %d' % position)
            position = next_position
        return items

    roots = read_list(_FBX_HEADER_BYTES, len(data))
    return FbxNode('', [version], roots), version


def _fbx_properties70(material_node):
    """The ``Properties70`` block of a Material as a plain dict."""
    values = {}
    block = material_node.first('Properties70')
    if block is None:
        return values
    for entry in block.all('P'):
        if not entry.properties:
            continue
        name = entry.properties[0]
        payload = entry.properties[4:]
        if len(payload) == 1:
            values[name] = payload[0]
        elif payload:
            values[name] = payload
    return values


def _fbx_strip_class(value):
    """Drop the FBX ``Class::`` / ``\\x00\\x01Class`` decoration from a name.

    A binary writer stores ``Barrel_01\\x00\\x01Model``; an ASCII writer stores
    ``Model::Barrel_01``.  Both mean the user-visible name ``Barrel_01``, and a
    binding that is reported against the decorated string cannot be compared
    with what the panel shows.
    """
    text = str(value or '')
    text = text.split('\x00')[0]
    if '::' in text:
        text = text.split('::', 1)[1]
    return text


def _fbx_scalar(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        return raw[1:-1].replace('\\"', '"') if raw.endswith('"') else raw[1:]
    if not raw:
        return ''
    try:
        if re.fullmatch(r'-?\d+', raw):
            return int(raw)
        return float(raw)
    except ValueError:
        return raw


def _fbx_scalar_list(rest):
    values = []
    token = []
    quoted = False
    for character in rest:
        if character == '"':
            quoted = not quoted
            token.append(character)
        elif character == ',' and not quoted:
            values.append(''.join(token))
            token = []
        else:
            token.append(character)
    if token:
        values.append(''.join(token))
    return [_fbx_scalar(item) for item in values if item.strip() != '']


def _fbx_ascii_tree(text):
    """Parse an ASCII FBX into the same node tree the binary reader builds.

    Real corpora are full of ASCII FBX (assimp, several DCC exporters), so a
    reader that only understands the binary container silently reports "no
    materials" for a file that has them.  Only the container differs: the
    object/connection semantics are identical, so both paths share
    :func:`_fbx_facts_from_tree`.
    """
    root = FbxNode('', [], [])
    stack = [root]
    array = None
    for raw_line in text.splitlines():
        line = _fbx_strip_comment(raw_line).strip()
        if not line:
            continue
        if array is not None:
            if line == '}':
                array = None
                continue
            body = line
            if body[:2].lower() == 'a:':
                body = body[2:]
            for piece in body.rstrip(',').split(','):
                if piece.strip():
                    array.append(_fbx_scalar(piece))
            continue
        if line == '}':
            if len(stack) > 1:
                stack.pop()
            continue
        if line.startswith('*'):
            continue                       # stray array body: already consumed
        match = re.match(r'^([^:\s]+)\s*:\s*(.*)$', line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2).strip()
        opens = rest.endswith('{')
        if opens:
            rest = rest[:-1].strip()
        if re.fullmatch(r'\*\d+', rest):
            values = []
            node = FbxNode(name, [values], [])
            stack[-1].children.append(node)
            array = values
            continue
        node = FbxNode(name, _fbx_scalar_list(rest), [])
        stack[-1].children.append(node)
        if opens:
            stack.append(node)
    return root


def _fbx_strip_comment(line):
    """Remove a ``;`` comment without touching a quoted ``;``."""
    quoted = False
    for index, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == ';' and not quoted:
            return line[:index]
    return line


def parse_fbx_materials(path):
    """Material definitions, texture links and per-model bindings of an FBX."""
    facts = MaterialFacts(format='fbx', container='unknown')
    base_dir = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, 'rb') as stream:
            raw = stream.read()
        facts.sha256 = hashlib.sha256(raw).hexdigest()
    except Exception as error:                       # noqa: BLE001 - reported
        facts.ok = False
        facts.error = '%s: %s' % (type(error).__name__, error)
        return facts
    if raw.startswith(_FBX_MAGIC_TEXT) and not raw.startswith(_FBX_BINARY_MAGIC):
        facts.ok = False
        facts.error = 'malformed binary header: %r' % (raw[:len(_FBX_BINARY_MAGIC)],)
        facts.container = 'malformed-binary'
        return facts
    if not raw.startswith(_FBX_BINARY_MAGIC):
        facts.container = 'ascii'
        try:
            root = _fbx_ascii_tree(_decode_text(raw))
        except Exception as error:                   # noqa: BLE001 - reported
            facts.ok = False
            facts.error = 'ascii parse: %s: %s' % (type(error).__name__, error)
            return facts
        return _fbx_facts_from_tree(root, 0, base_dir, facts)
    facts.container = 'binary'
    try:
        root, version = _fbx_tree(raw)
    except Exception as error:                       # noqa: BLE001 - reported
        facts.ok = False
        facts.error = 'binary parse: %s: %s' % (type(error).__name__, error)
        return facts
    return _fbx_facts_from_tree(root, version, base_dir, facts)


def _fbx_facts_from_tree(root, version, base_dir, facts):
    """Material facts of an FBX node tree, whichever container produced it."""
    if version:
        facts.notes.append('FBX 版本 %d' % version)
    objects = root.first('Objects')
    connections = root.first('Connections')
    if objects is None:
        facts.ok = False
        facts.error = 'FBX 没有 Objects 块'
        return facts

    by_id = {}
    materials_by_id = {}
    geometry_by_id = {}
    model_by_id = {}
    texture_by_id = {}
    video_by_id = {}

    for node in objects.children:
        node_id = node.properties[0] if node.properties else None
        name = node.properties[1] if len(node.properties) > 1 else ''
        subtype = node.properties[2] if len(node.properties) > 2 else ''
        if not isinstance(node_id, int):
            continue
        by_id[node_id] = node
        if node.name == 'Material':
            materials_by_id[node_id] = node
        elif node.name == 'Geometry':
            geometry_by_id[node_id] = node
        elif node.name == 'Model':
            model_by_id[node_id] = node
        elif node.name == 'Texture':
            texture_by_id[node_id] = node
        elif node.name == 'Video':
            video_by_id[node_id] = node
        elif node.name in ('NodeAttribute',):
            pass
        elif node.name not in ('Pose', 'Deformer', 'AnimationStack',
                               'AnimationLayer', 'AnimationCurveNode',
                               'AnimationCurve', 'Collection', 'CollectionExclusive'):
            facts.notes.append('未处理的 FBX 对象类型：%s(%s)' % (node.name, subtype))

    links = []                     # (kind, child, parent, property)
    if connections is not None:
        for entry in connections.all('C'):
            properties = entry.properties
            if len(properties) < 3:
                continue
            kind = properties[0] if isinstance(properties[0], str) else '?'
            child, parent = properties[1], properties[2]
            prop = properties[3] if len(properties) > 3 else ''
            links.append((kind, child, parent, prop))

    # texture -> material slots
    texture_slots = {}
    for kind, child, parent, prop in links:
        if child in texture_by_id and parent in materials_by_id:
            texture_slots.setdefault(child, []).append((parent, prop))
        if child in video_by_id and parent in texture_by_id:
            video_by_id[child].properties = video_by_id[child].properties
    video_of_texture = {}
    for kind, child, parent, prop in links:
        if child in video_by_id and parent in texture_by_id:
            video_of_texture[parent] = child

    def texture_declarations(texture_node):
        """Every path declaration an FBX texture carries, with its source.

        FBX writes ``FileName`` (usually the author machine's absolute path) on
        the ``Video`` object and ``RelativeFilename`` (the deployable one) on
        the ``Texture`` object.  Both are declarations; neither is "the path".
        Keeping only one silently redefines what "missing texture" means, and
        loses the author path the user may need to remap.
        """
        declarations = []
        seen = set()
        for container in (texture_node,
                          by_id.get(video_of_texture.get(id(texture_node), -1))):
            if container is None:
                continue
            for child in container.children:
                if child.name not in ('FileName', 'RelativeFilename'):
                    continue
                if not child.properties:
                    continue
                value = child.properties[0]
                if not isinstance(value, str) or not value:
                    continue
                key = (id(container), child.name, value)
                if key in seen:
                    continue
                seen.add(key)
                declarations.append({
                    'object_kind': container.name,
                    'object_id': (container.properties[0]
                                  if container.properties
                                  and isinstance(container.properties[0], int) else None),
                    'object_name': _fbx_strip_class(
                        container.properties[1] if len(container.properties) > 1 else ''),
                    'field': child.name,
                    'value': value,
                    'absolute': is_absolute_reference(value),
                })
        return declarations

    material_models = {}
    for kind, child, parent, prop in links:
        if child in materials_by_id and parent in model_by_id and kind == 'OO':
            material_models.setdefault(child, []).append(parent)
        if child in materials_by_id and parent in model_by_id and kind == 'OP':
            material_models.setdefault(child, []).append(parent)

    # model -> geometry, model -> model (instance / hierarchy)
    model_geometry = {}
    model_parent = {}
    for kind, child, parent, prop in links:
        if child in geometry_by_id and parent in model_by_id:
            model_geometry[parent] = child
        if kind == 'OO' and child in model_by_id and parent in model_by_id:
            model_parent[child] = parent

    def model_path(model_id):
        names = []
        cursor = model_id
        seen = set()
        while cursor in model_by_id and cursor not in seen:
            seen.add(cursor)
            node = model_by_id[cursor]
            names.append(_fbx_strip_class(node.properties[1])
                         if len(node.properties) > 1 else str(cursor))
            cursor = model_parent.get(cursor)
        return '/' + '/'.join(reversed(names))

    # material indices per model follow the connection order, which is the
    # order the FBX SDK assigns material slots in.
    model_materials = {}
    for model_id in model_by_id:
        slots = [material_id for material_id, models in material_models.items()
                 if model_id in models]
        model_materials[model_id] = slots

    for material_id, node in materials_by_id.items():
        properties = _fbx_properties70(node)
        definition = MaterialDefinition(
            index=len(facts.materials),
            name=(_fbx_strip_class(node.properties[1])
                  if len(node.properties) > 1 else str(material_id)),
            locator='fbx:Material[%d]' % material_id,
            workflow='fbx_properties70',
        )
        for key in ('DiffuseColor', 'SpecularColor', 'EmissiveColor', 'AmbientColor',
                    'TransparencyFactor', 'Opacity', 'Shininess', 'ShininessExponent',
                    'SpecularFactor', 'ReflectionFactor', 'Metallic', 'Roughness',
                    'Maya|baseColor', 'Maya|metallic', 'Maya|roughness',
                    'DiffuseFactor', 'EmissiveFactor'):
            if key in properties:
                definition.properties[key] = properties[key]
        shading = node.first('ShadingModel')
        if shading is not None and shading.properties:
            definition.workflow = str(shading.properties[0])
        for texture_id, slots in _fbx_textures_of(texture_slots, material_id):
            declarations = texture_declarations(texture_by_id[texture_id])
            for slot_property in slots:
                slot, channels, color_space = _fbx_slot_for(slot_property)
                # The reference is filled in below from the declarations:
                # a texture use starts with "nothing adopted yet".
                use = TextureUse(slot=slot, reference='', channels=channels,
                                 color_space=color_space)
                if slot == 'normal':
                    use.normal_convention = 'normal-map:convention-unknown'
                elif slot == 'bump':
                    use.normal_convention = 'height'
                use.declarations = declarations
                # 每一处声明都解析一遍，并记下为什么没被采用。
                candidates = []
                for declaration in declarations:
                    value = declaration['value']
                    resolved = _resolve(base_dir, value)
                    exists = bool(resolved and os.path.isfile(resolved))
                    candidates.append({
                        'field': declaration['field'],
                        'object_kind': declaration['object_kind'],
                        'value': value,
                        'absolute': declaration['absolute'],
                        'resolved': (urllib.parse.unquote(value)
                                     if declaration['absolute']
                                     else (_relative(base_dir, resolved)
                                           if resolved else '')),
                        'exists': exists,
                        'digest': (DIGEST_PREFIX + sha256_file(resolved))
                                  if exists else '',
                    })
                # 相对路径优先，其次绝对路径；只有真实存在的候选才会被采用。
                ordered = sorted(candidates,
                                 key=lambda item: (item['absolute'],
                                                   0 if item['field'] == 'RelativeFilename' else 1))
                adopted = next((item for item in ordered if item['exists']), None)
                use.candidates = candidates
                if adopted is None:
                    # 一个都不在：不采用任何文件，也不把首选字符串当成采用。
                    use.exists = False
                    use.selected_resource_id = ''
                    use.selected_path = ''
                    use.reference = declarations[0]['value'] if declarations else ''
                    use.resolved = (candidates[0]['resolved'] if candidates else '')
                    use.absolute = bool(candidates and candidates[0]['absolute'])
                    use.selection_reason = ('声明了 %d 条路径，本机都不存在，未采用任何文件'
                                            % len(declarations)) if declarations else '没有路径声明'
                else:
                    use.exists = True
                    use.selected_resource_id = adopted['digest']
                    use.selected_path = adopted['resolved']
                    use.reference = adopted['value']
                    use.resolved = adopted['resolved']
                    use.absolute = adopted['absolute']
                    use.selection_reason = (
                        '采用 %s 的 %s 字段（本机存在）'
                        % (adopted['object_kind'], adopted['field']))
                    if adopted['field'] != declarations[0]['field']:
                        others = [item for item in candidates
                                  if item is not adopted and item['exists']]
                        use.selection_reason += (
                            '；另一条声明也存在，按相对路径优先' if others
                            else '；首条声明在本机不可用')
                existing = [item for item in candidates if item['exists']]
                if len({item['digest'] for item in existing}) > 1:
                    use.candidate_conflict = True
                    facts.notes.append(
                        '%s 的 %s：多条声明解析到不同文件，已采用 %s，冲突需人工确认'
                        % (definition.name, slot_property,
                           use.selected_path or '(无)'))
                use.extra['fbx_texture_id'] = texture_id
                use.extra['fbx_property'] = slot_property
                use.extra['fbx_declaration_count'] = len(declarations)
                use.extra['fbx_declaration_fields'] = [item['field']
                                                       for item in declarations]
                if len(declarations) > 1:
                    use.extra['fbx_multi_declaration'] = True
                    facts.notes.append(
                        '%s 的 %s 声明了 %d 条路径（%s），采用理由：%s'
                        % (definition.name, slot_property, len(declarations),
                           ', '.join(item['field'] for item in declarations),
                           use.selection_reason))
                definition.textures.append(use)
        facts.materials.append(definition)
    material_index_by_id = {material_id: definition.index for material_id, definition
                            in zip(materials_by_id, facts.materials)}

    # per-model, per-slot binding sites; the polygon split comes from
    # LayerElementMaterial when the file carries one.
    for model_id, slots in model_materials.items():
        geometry_id = model_geometry.get(model_id)
        polygon_counts = {}
        split_basis = 'file:material-slot'
        if geometry_id is not None:
            polygon_counts, split_basis = _fbx_polygon_material_counts(
                geometry_by_id[geometry_id], len(slots))
        path = model_path(model_id)
        for slot_index, material_id in enumerate(slots):
            facts.sites.append(BindingSite(
                site='fbx:%s#slot[%d]' % (path, slot_index),
                node_path=path,
                material_index=material_index_by_id.get(material_id),
                material_name=(facts.materials[material_index_by_id[material_id]].name
                               if material_id in material_index_by_id else ''),
                face_count=polygon_counts.get(slot_index),
                primitive_index=slot_index,
                geometry_key='Geometry[%s]' % geometry_id if geometry_id is not None else '',
                basis=split_basis,
            ))
    for model_id in model_by_id:
        if model_id not in model_materials:
            facts.sites.append(BindingSite(
                site='fbx:%s#no-material' % model_path(model_id),
                node_path=model_path(model_id),
                material_index=None,
                basis='file:model-without-material',
            ))
    used = {site.material_index for site in facts.sites
            if site.material_index is not None}
    facts.unused_materials = [material_id for material_id, definition in zip(
        materials_by_id, facts.materials) if definition.index not in used]
    names = [material.name for material in facts.materials]
    facts.duplicate_material_names = sorted({name for name in names
                                             if names.count(name) > 1})
    uv_sets = 0
    for geometry_id, node in geometry_by_id.items():
        uv_sets += len(node.all('LayerElementUV'))
    facts.uv_sets = uv_sets or None
    dependencies = []
    missing = []
    absolute = []
    embedded = []
    for material in facts.materials:
        for use in material.textures:
            if use.absolute and use.reference:
                absolute.append(urllib.parse.unquote(use.reference))
            if use.resolved and not use.absolute:
                dependencies.append(use.resolved)
                if not use.exists:
                    missing.append(use.resolved)
    for video_id, node in video_by_id.items():
        content = node.first('Content')
        if content is not None and content.properties:
            embedded.append('Video[%d]:Content(%d bytes)'
                            % (video_id, len(content.properties[0])
                               if isinstance(content.properties[0], (bytes, list)) else -1))
    facts.dependencies = tuple(sorted(set(dependencies)))
    facts.missing_dependencies = tuple(sorted(set(missing)))
    facts.absolute_references = tuple(sorted(set(absolute)))
    facts.embedded_media = tuple(sorted(set(embedded)))
    facts.basis = 'file:fbx-node-tree'
    if not materials_by_id:
        facts.notes.append('文件没有 Material 对象')
    return facts


def _fbx_textures_of(texture_slots, material_id):
    """Yield (texture_id, [property names]) for one material."""
    collected = {}
    for texture_id, slots in texture_slots.items():
        for parent, prop in slots:
            if parent == material_id:
                collected.setdefault(texture_id, []).append(prop or 'unknown')
    for texture_id, slots in collected.items():
        yield texture_id, slots


def _fbx_polygon_material_counts(geometry_node, slot_count):
    """How many polygons each material slot owns, when the file declares it."""
    layer = None
    for child in geometry_node.all('LayerElementMaterial'):
        layer = child
        break
    if layer is None:
        return {}, 'file:material-slot'
    mapping = layer.first('MappingInformationType')
    materials = layer.first('Materials')
    if materials is None or not materials.properties:
        return {}, 'file:material-slot'
    values = materials.properties[0]
    if not isinstance(values, list):
        return {}, 'file:material-slot'
    mapping_type = str(mapping.properties[0]) if mapping is not None else ''
    counts = {}
    if mapping_type == 'ByPolygon':
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        return counts, 'file:layer-element-material-by-polygon'
    if mapping_type == 'AllSame':
        if values:
            counts[values[0]] = None
        return counts, 'file:layer-element-material-all-same'
    return {}, 'file:material-slot(%s)' % (mapping_type or 'unknown')


# ── dispatcher ────────────────────────────────────────────────────────────
_READERS = {'.gltf': parse_gltf_materials, '.glb': parse_gltf_materials,
            '.obj': parse_obj_materials, '.fbx': parse_fbx_materials}


def read_material_facts(path):
    """Read one model file and report its material facts."""
    extension = os.path.splitext(str(path))[1].lower()
    reader = _READERS.get(extension)
    if reader is None:
        facts = MaterialFacts(format=extension.lstrip('.'), ok=False,
                              error='no reference reader for %s' % extension)
        facts.sureness = 'none'
        return facts
    try:
        return reader(path)
    except Exception as error:                       # noqa: BLE001 - reported
        facts = MaterialFacts(format=extension.lstrip('.'), ok=False,
                              error='reader failure: %s: %s'
                                    % (type(error).__name__, error))
        facts.sureness = 'none'
        return facts


def dependency_hashes(base_dir, relative_paths):
    """SHA-256 of every file a model refers to, so a fixture cannot drift."""
    hashes = {}
    for relative in relative_paths:
        target = os.path.join(base_dir, relative.replace('/', os.sep))
        hashes[relative] = sha256_file(target) if os.path.isfile(target) else None
    return hashes
