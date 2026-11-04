"""glTF 2.0 and GLB: definitions, texture uses and instance-level binding sites.

What this reader reports comes from the document itself.  It never infers a
material from a file name, never expands UVs, never rewrites the binary and
never downloads a remote reference.

Two facts are deliberately kept apart:

* the **document** (``materials``, ``textures``, ``images``, ``samplers``,
  ``mesh.primitives``) which is a fixed part of the source revision, and
* the **instances** (``nodes`` inside ``scenes``), because one mesh used four
  times is four binding sites that must stay independently addressable.

An embedded image gets the SHA-256 of its decoded bytes, and a ``bufferView``
image is hashed from the buffer region it names, so identity is the bytes --
also for data URIs and for the GLB binary chunk.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

from ..catalog import (
    CHANNEL_B,
    CHANNEL_G,
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

PARSER_ID_GLTF = 'gltf-2.0'
PARSER_ID_GLB = 'glb-2.0'

#: A glTF normal map follows the glTF specification: tangent space, +Y up,
#: which is the OpenGL convention.  It is recorded, never assumed silently.
NORMAL_CONVENTION = 'opengl'

#: ``normalTexture.scale`` and ``occlusionTexture.strength`` defaults the
#: specification states.  They are written only when the source states them.
_TEXTURE_INFO_SLOTS = (
    # (container, key, slot, channels, colour space)
    ('pbr', 'baseColorTexture', 'basecolor', CHANNEL_RGB, COLOR_SRGB),
    ('pbr', 'metallicRoughnessTexture', 'metallic', CHANNEL_B, COLOR_LINEAR),
    ('pbr', 'metallicRoughnessTexture', 'roughness', CHANNEL_G, COLOR_LINEAR),
    ('material', 'normalTexture', 'normal', CHANNEL_RGB, COLOR_LINEAR),
    ('material', 'occlusionTexture', 'ao', CHANNEL_R, COLOR_LINEAR),
    ('material', 'emissiveTexture', 'emissive', CHANNEL_RGB, COLOR_SRGB),
)

#: Extensions this reader understands.  Everything else is reported in
#: ``unsupported`` so a viewer never claims the material is complete.
KNOWN_EXTENSIONS = frozenset({
    'KHR_texture_transform',
    'KHR_materials_unlit',
    'KHR_materials_pbrSpecularGlossiness',
})

_JSON_CHUNK = 0x4E4F534A
_BIN_CHUNK = 0x004E4942


def inspect_gltf(main_path, *, revision: int = 1):
    """Read a ``.gltf`` document and its declared siblings."""
    path = Path(main_path)
    try:
        document = json.loads(path.read_text(encoding='utf-8-sig'))
    except OSError as error:
        raise CatalogParseError(f'glTF 文件无法读取：{error}') from error
    except ValueError as error:
        raise CatalogParseError(f'glTF 文档不是合法 JSON：{error}') from error
    return _inspect(document, path, PARSER_ID_GLTF, revision=revision)


def inspect_glb(main_path, *, revision: int = 1):
    """Read a ``.glb`` container: JSON chunk plus (optional) BIN chunk."""
    path = Path(main_path)
    document, bin_chunk = _read_glb(path)
    return _inspect(document, path, PARSER_ID_GLB, revision=revision,
                    bin_chunk=bin_chunk)


def _read_glb(path: Path) -> tuple:
    try:
        with path.open('rb') as handle:
            header = handle.read(12)
            if len(header) < 12:
                raise CatalogParseError('GLB 文件短于其头部，无法读取')
            magic = header[:4]
            version = int.from_bytes(header[4:8], 'little')
            if magic != b'glTF':
                raise CatalogParseError('文件不是 GLB 容器')
            if version != 2:
                raise CatalogParseError(f'不支持的 GLB 版本：{version}')
            document = None
            bin_chunk = b''
            while True:
                chunk_header = handle.read(8)
                if len(chunk_header) < 8:
                    break
                length = int.from_bytes(chunk_header[:4], 'little')
                kind = int.from_bytes(chunk_header[4:8], 'little')
                data = handle.read(length)
                if len(data) < length:
                    raise CatalogParseError('GLB 容器被截断')
                if kind == _JSON_CHUNK and document is None:
                    document = json.loads(data.decode('utf-8'))
                elif kind == _BIN_CHUNK and not bin_chunk:
                    bin_chunk = data
    except OSError as error:
        raise CatalogParseError(f'GLB 文件无法读取：{error}') from error
    except ValueError as error:
        raise CatalogParseError(f'GLB 的 JSON 块不是合法 JSON：{error}') from error
    if document is None:
        raise CatalogParseError('GLB 容器里没有 JSON 块')
    return document, bin_chunk


def _inspect(document, main_path: Path, parser_id: str, *, revision: int,
             bin_chunk: bytes = b''):
    if not isinstance(document, dict):
        raise CatalogParseError('glTF 文档不是对象')
    base_dir = main_path.parent
    builder = CatalogBuilder(parser_id, main_path=main_path)
    extensions_used = tuple(str(item) for item in document.get('extensionsUsed') or ())
    builder.note_unknown('extensions_used', list(extensions_used))
    builder.note_unknown('generator', str((document.get('asset') or {}).get('generator') or ''))
    builder.note_unknown('version', str((document.get('asset') or {}).get('version') or ''))

    materials = document.get('materials') or []
    for index, material in enumerate(materials):
        if not isinstance(material, dict):
            builder.note_unresolved(f'materials[{index}] 不是对象，已跳过')
            continue
        _add_definition(builder, index, material, document, base_dir, bin_chunk)

    _add_binding_sites(builder, document, len(materials))
    _note_unreadable(document, builder)
    return builder.build(revision=revision)


# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------
def _add_definition(builder: CatalogBuilder, index: int, material: dict,
                    document: dict, base_dir: Path, bin_chunk: bytes) -> None:
    material_id = f'gltf:material:{index}'
    name = str(material.get('name') or '')
    workflow, unsupported = _workflow(material)
    properties = _properties(material)
    textures = _texture_uses(builder, index, material, document, base_dir, bin_chunk)
    builder.add_definition(MaterialDefinition(
        material_id=material_id,
        source_key=f'materials[{index}]',
        name=name,
        workflow=workflow,
        textures=tuple(textures),
        properties_json=json.dumps(properties, ensure_ascii=False, sort_keys=True),
        unsupported=tuple(unsupported),
        # The whole source object is retained, so a field this reader does not
        # map is still there after a save and a reload.
        unknown_json=json.dumps({'source': material}, ensure_ascii=False,
                                sort_keys=True),
    ))


def _workflow(material: dict) -> tuple:
    extensions = material.get('extensions') or {}
    unsupported = []
    for key in extensions:
        if key not in KNOWN_EXTENSIONS:
            unsupported.append(f'未解释的扩展：{key}')
    if 'KHR_materials_unlit' in extensions:
        return 'gltf_unlit', unsupported
    if 'KHR_materials_pbrSpecularGlossiness' in extensions:
        unsupported.append('KHR_materials_pbrSpecularGlossiness 的镜面/光泽度未分解为金属度/粗糙度')
        return 'gltf_specular_glossiness', unsupported
    if 'pbrMetallicRoughness' in material:
        return 'gltf_pbr_metallic_roughness', unsupported
    return 'gltf_default_material', unsupported


def _properties(material: dict) -> dict:
    """Only the fields the source actually states, plus an explicit index."""
    properties: dict = {}
    present = []
    pbr = material.get('pbrMetallicRoughness')
    if isinstance(pbr, dict):
        if 'baseColorFactor' in pbr:
            properties['basecolor_factor'] = list(pbr['baseColorFactor'])
            present.append('pbrMetallicRoughness.baseColorFactor')
        if 'metallicFactor' in pbr:
            properties['metallic_factor'] = float(pbr['metallicFactor'])
            present.append('pbrMetallicRoughness.metallicFactor')
        if 'roughnessFactor' in pbr:
            properties['roughness_factor'] = float(pbr['roughnessFactor'])
            present.append('pbrMetallicRoughness.roughnessFactor')
    if 'normalTexture' in material and isinstance(material['normalTexture'], dict):
        if 'scale' in material['normalTexture']:
            properties['normal_scale'] = float(material['normalTexture']['scale'])
            present.append('normalTexture.scale')
    if 'occlusionTexture' in material and isinstance(material['occlusionTexture'], dict):
        if 'strength' in material['occlusionTexture']:
            properties['occlusion_strength'] = float(material['occlusionTexture']['strength'])
            present.append('occlusionTexture.strength')
    if 'emissiveFactor' in material:
        properties['emissive_factor'] = list(material['emissiveFactor'])
        present.append('emissiveFactor')
    if 'alphaMode' in material:
        properties['alpha_mode'] = str(material['alphaMode'])
        present.append('alphaMode')
    if 'alphaCutoff' in material:
        properties['alpha_cutoff'] = float(material['alphaCutoff'])
        present.append('alphaCutoff')
    if 'doubleSided' in material:
        properties['double_sided'] = bool(material['doubleSided'])
        present.append('doubleSided')
    properties['present'] = sorted(present)
    return properties


def _texture_uses(builder: CatalogBuilder, index: int, material: dict,
                  document: dict, base_dir: Path, bin_chunk: bytes) -> list:
    uses = []
    pbr = material.get('pbrMetallicRoughness')
    pbr = pbr if isinstance(pbr, dict) else {}
    seen = set()
    for container, key, slot, channels, color_space in _TEXTURE_INFO_SLOTS:
        source = pbr if container == 'pbr' else material
        info = source.get(key)
        if not isinstance(info, dict):
            continue
        identity = (key, slot)
        if identity in seen:
            continue
        seen.add(identity)
        use = _texture_use(builder, index, slot, info, document, base_dir,
                           bin_chunk, channels, color_space,
                           normal_convention=NORMAL_CONVENTION if slot == 'normal' else '',
                           label=f'materials[{index}].{key}')
        if use is not None:
            uses.append(use)
    # The specular/glossiness extension keeps a diffuse map; report it as a
    # basecolour texture but say the glossiness map was not decomposed.
    extension = ((material.get('extensions') or {}).get(
        'KHR_materials_pbrSpecularGlossiness') or {})
    if isinstance(extension, dict):
        diffuse = extension.get('diffuseTexture')
        if isinstance(diffuse, dict):
            use = _texture_use(builder, index, 'basecolor', diffuse, document,
                               base_dir, bin_chunk, CHANNEL_RGB, COLOR_SRGB,
                               label=f'materials[{index}].diffuseTexture')
            if use is not None:
                uses.append(use)
    return uses


def _texture_use(builder: CatalogBuilder, material_index: int, slot: str,
                 info: dict, document: dict, base_dir: Path, bin_chunk: bytes,
                 channels: str, color_space: str, *, normal_convention: str = '',
                 label: str) -> Optional['object']:
    texture_index = info.get('index')
    textures = document.get('textures') or []
    if not isinstance(texture_index, int) or not (0 <= texture_index < len(textures)):
        builder.note_unresolved(f'{label} 指向不存在的 texture：{texture_index!r}')
        return None
    texture = textures[texture_index] if isinstance(textures[texture_index], dict) else {}
    unknown: dict = {'texture_index': texture_index}
    texcoord = info.get('texCoord', 0)
    uv_transform: tuple = ()
    extensions = info.get('extensions') or {}
    for key, value in extensions.items():
        if key != 'KHR_texture_transform':
            builder.note_unresolved(f'{label} 使用了未解释的扩展：{key}')
            unknown[key] = value
            continue
        if not isinstance(value, dict):
            continue
        offset = value.get('offset') or (0.0, 0.0)
        scale = value.get('scale') or (1.0, 1.0)
        uv_transform = (float(offset[0]), float(offset[1]),
                        float(value.get('rotation', 0.0) or 0.0),
                        float(scale[0]), float(scale[1]))
        if 'texCoord' in value:
            texcoord = value['texCoord']
        unknown['KHR_texture_transform'] = value
    if 'texCoord' in texture:
        # Older exporters wrote the UV set on the texture instead of the
        # textureInfo; report the discrepancy instead of silently choosing.
        builder.note_unresolved(
            f'{label} 在 texture 上写了 texCoord，与 textureInfo 的 UV 集不同，'
            '按 textureInfo 记录')
    sampler_index = texture.get('sampler')
    if isinstance(sampler_index, int):
        samplers = document.get('samplers') or []
        if 0 <= sampler_index < len(samplers) and isinstance(samplers[sampler_index], dict):
            unknown['sampler'] = samplers[sampler_index]
    resource, note = _image_resource(builder, texture, document, base_dir, bin_chunk,
                                     label)
    if note:
        builder.note_unresolved(note)
    builder.note_resource_digest(resource.locator, resource.resource_id)
    unknown['embedded'] = bool(resource.embedded)
    if resource.missing:
        unknown['missing'] = True
    if resource.remote:
        unknown['remote'] = True
    return texture_use(slot, resource, channels=channels, texcoord=_int(texcoord),
                       color_space=color_space, uv_transform=uv_transform,
                       normal_convention=normal_convention,
                       source_locator=resource.locator, unknown=unknown)


def _image_resource(builder: CatalogBuilder, texture: dict, document: dict,
                    base_dir: Path, bin_chunk: bytes, label: str) -> tuple:
    images = document.get('images') or []
    image_index = texture.get('source')
    if not isinstance(image_index, int) or not (0 <= image_index < len(images)):
        return (ResourceRef(missing=True, locator=f'{label} 没有 image'),
                f'{label} 没有可用的 image 索引：{image_index!r}')
    image = images[image_index] if isinstance(images[image_index], dict) else {}
    if 'uri' in image:
        return locators.read_reference(str(image['uri']), base_dir)
    if 'bufferView' in image:
        view = image.get('bufferView')
        data = _buffer_view_bytes(document, view, base_dir, bin_chunk)
        locator_text = f'{label}.images[{image_index}].bufferView:{view}'
        if data is None:
            return (ResourceRef(missing=True, locator=locator_text),
                    f'{locator_text} 的数据块无法读取')
        return (ResourceRef(resource_id=resource_id_of(data), locator=locator_text,
                            embedded=True, size_bytes=len(data)), '')
    return (ResourceRef(missing=True, locator=f'{label}.images[{image_index}]'),
            f'{label} 的 image 既没有 uri 也没有 bufferView')


def _buffer_view_bytes(document: dict, view_index, base_dir: Path,
                       bin_chunk: bytes) -> Optional[bytes]:
    views = document.get('bufferViews') or []
    if not isinstance(view_index, int) or not (0 <= view_index < len(views)):
        return None
    view = views[view_index] if isinstance(views[view_index], dict) else {}
    buffer_index = view.get('buffer')
    buffers = document.get('buffers') or []
    if not isinstance(buffer_index, int) or not (0 <= buffer_index < len(buffers)):
        return None
    buffer = buffers[buffer_index] if isinstance(buffers[buffer_index], dict) else {}
    offset = int(view.get('byteOffset', 0) or 0)
    length = int(view.get('byteLength', 0) or 0)
    if length <= 0 or length > locators.MAX_INSPECT_BYTES:
        return None
    uri = buffer.get('uri')
    if uri is None:
        if buffer_index != 0 or not bin_chunk:
            return None
        return bytes(bin_chunk[offset:offset + length])
    text = str(uri)
    if text.startswith('data:'):
        decoded = locators.read_bytes(text, base_dir)
        if decoded is None:
            return None
        return decoded[offset:offset + length]
    locator = locators.resolve_locator(text, base_dir)
    if not locator.resolved:
        return None
    try:
        with Path(locator.path).open('rb') as handle:
            handle.seek(offset)
            data = handle.read(length)
    except OSError:
        return None
    return data if len(data) == length else None


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# binding sites
# ---------------------------------------------------------------------------
def _add_binding_sites(builder: CatalogBuilder, document: dict,
                       material_count: int) -> None:
    from ..contracts import BindingSite

    nodes = document.get('nodes') or []
    meshes = document.get('meshes') or []
    scenes = document.get('scenes') or []
    used_meshes = set()

    def material_id(value):
        if isinstance(value, int) and 0 <= value < material_count:
            return f'gltf:material:{value}'
        return None

    def walk(node_index: int, path: str, scene_key: str, stack: tuple) -> None:
        if not isinstance(node_index, int) or not (0 <= node_index < len(nodes)):
            return
        if node_index in stack:
            builder.note_unresolved(f'节点图存在环：node[{node_index}]')
            return
        node = nodes[node_index]
        if not isinstance(node, dict):
            builder.note_unresolved(f'nodes[{node_index}] 不是对象，已跳过')
            return
        name = str(node.get('name') or f'node[{node_index}]')
        node_path = f'{path}/{name}' if path else name
        mesh_index = node.get('mesh')
        if isinstance(mesh_index, int) and 0 <= mesh_index < len(meshes):
            used_meshes.add(mesh_index)
            mesh = meshes[mesh_index] if isinstance(meshes[mesh_index], dict) else {}
            primitives = mesh.get('primitives') or []
            for primitive_index, primitive in enumerate(primitives):
                primitive = primitive if isinstance(primitive, dict) else {}
                assigned = primitive.get('material')
                original = material_id(assigned)
                resolution = 'assigned' if original else 'unassigned'
                if assigned is not None and original is None:
                    resolution = 'dangling'
                    builder.note_unresolved(
                        f'mesh[{mesh_index}].primitives[{primitive_index}] 的材质索引越界：'
                        f'{assigned!r}；使用点保留，分配为悬空')
                binding_id = (f'gltf|scene:{scene_key}|node:{node_index}'
                              f'|mesh:{mesh_index}|primitive:{primitive_index}')
                builder.add_binding(BindingSite(
                    binding_id=binding_id,
                    source_key=f'mesh:{mesh_index}|primitive:{primitive_index}',
                    node_path=node_path,
                    geometry_key=f'gltf:mesh:{mesh_index}',
                    primitive_key=f'primitive:{primitive_index}',
                    original_material_id=original))
                builder.note_binding_resolution(binding_id, resolution)
        elif mesh_index is not None:
            builder.note_unresolved(f'node[{node_index}] 指向不存在的 mesh：{mesh_index!r}')
        for child in node.get('children') or ():
            walk(child, node_path, scene_key, stack + (node_index,))

    child_nodes = {child for node in nodes if isinstance(node, dict)
                   for child in (node.get('children') or ())}
    if scenes:
        for scene_index, scene in enumerate(scenes):
            scene = scene if isinstance(scene, dict) else {}
            name = str(scene.get('name') or f'scene[{scene_index}]')
            for root in scene.get('nodes') or ():
                walk(root, name, str(scene_index), ())
    else:
        roots = [index for index in range(len(nodes)) if index not in child_nodes]
        for root in roots:
            walk(root, '', 'no-scene', ())
        if not roots and nodes:
            builder.note_unresolved('文档没有任何场景，也没有可用的根节点')
    for mesh_index in range(len(meshes)):
        if mesh_index not in used_meshes:
            builder.note_unresolved(f'mesh[{mesh_index}] 没有被任何节点实例引用')


def _note_unreadable(document: dict, builder: CatalogBuilder) -> None:
    """Report the parts of the document a catalog cannot represent yet."""
    if document.get('animations'):
        builder.note_unknown('animations', len(document['animations']))
    for index, node in enumerate(document.get('nodes') or ()):
        if not isinstance(node, dict):
            continue
        if 'skin' in node:
            builder.note_unresolved(f'node[{index}] 使用了蒙皮，材质清单不涉及蒙皮权重')
    if (document.get('asset') or {}).get('minVersion'):
        builder.note_unknown('min_version', str(document['asset']['minVersion']))


def embedded_image_bytes(uri: str, base_dir=None) -> Optional[bytes]:
    """Decode one ``data:`` image; exposed for tests and diagnostics only."""
    text = str(uri or '')
    if not text.startswith('data:'):
        return None
    _header, _, payload = text.partition(',')
    try:
        return base64.b64decode(payload, validate=False)
    except Exception:  # pragma: no cover - defensive
        return None
