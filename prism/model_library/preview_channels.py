"""Read-only channel diagnostics. Originals and appearance are never rewritten.

glTF diagnostic copies retain original geometry/buffer bytes and per-material UV
bindings. Single-material FBX diagnostics use declared OP texture connections.
Ambiguous legacy bindings are reported rather than guessed from filenames.
"""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import urllib.parse

from PIL import Image

CHANNELS = ('basecolor', 'normal', 'roughness', 'metallic', 'ao', 'opacity', 'emissive', 'sss', 'displacement')


def _data_factor(value):
    value = min(1., max(0., float(value)))
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


def read_gltf(path):
    path = Path(path)
    with path.open('rb') as f:
        head = f.read(12)
        if head[:4] != b'glTF':
            f.seek(0)
            return json.load(f), None
        _, version, length = struct.unpack('<4sII', head)
        if version != 2 or length != path.stat().st_size:
            raise ValueError('GLB 容器长度或版本无效')
        doc, binary = None, None
        while f.tell() < length:
            size, kind = struct.unpack('<II', f.read(8))
            data = f.read(size)
            if len(data) != size:
                raise ValueError('GLB 数据截断')
            if kind == 0x4E4F534A:
                doc = json.loads(data)
            elif kind == 0x004E4942:
                binary = data
        if doc is None:
            raise ValueError('GLB 缺少 JSON')
        return doc, binary


def _uri_bytes(uri, parent):
    if uri.startswith('data:'):
        head, data = uri.split(',', 1)
        return base64.b64decode(data) if ';base64' in head else urllib.parse.unquote_to_bytes(data)
    if urllib.parse.urlparse(uri).scheme:
        raise ValueError('通道查看不下载远程依赖')
    return (parent / urllib.parse.unquote(uri)).read_bytes()


def _image_bytes(doc, binary, index, parent):
    image = doc['images'][index]
    if 'uri' in image:
        return _uri_bytes(image['uri'], parent)
    view = doc['bufferViews'][image['bufferView']]
    buffer = doc['buffers'][view['buffer']]
    data = _uri_bytes(buffer['uri'], parent) if 'uri' in buffer else binary
    start = view.get('byteOffset', 0)
    result = data[start:start + view['byteLength']]
    if len(result) != view['byteLength']:
        raise ValueError('贴图 bufferView 越界')
    return result


def _link(source, destination):
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        import shutil
        shutil.copyfile(source, destination)


def gltf_channel(path, channel, cache):
    path, cache = Path(path), Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    doc, binary = read_gltf(path)
    implicit = [primitive for mesh in doc.get('meshes', [])
                for primitive in mesh.get('primitives', []) if 'material' not in primitive]
    if implicit:
        default_index = len(doc.setdefault('materials', []))
        doc['materials'].append({})
        for primitive in implicit:
            primitive['material'] = default_index
    # Every external URI gets an explicit local dependency, never an absolute
    # file URI whose interpretation differs between glTF readers.
    for entry in doc.get('buffers', []) + doc.get('images', []):
        uri = entry.get('uri', '')
        if not uri or uri.startswith('data:'):
            continue
        if urllib.parse.urlparse(uri).scheme:
            raise ValueError('通道查看不下载远程依赖')
        source = (path.parent / urllib.parse.unquote(uri)).resolve()
        name = 'deps/' + hashlib.sha256(str(source).encode()).hexdigest() + source.suffix
        _link(source, cache / name)
        entry['uri'] = name
    original = copy.deepcopy(doc)
    # Decode embedded images against the original buffer data, and external
    # images against the explicit dependency copies made above.
    for material in doc.get('materials', []):
        pbr = material.get('pbrMetallicRoughness', {})
        info, component, factor = None, None, [1, 1, 1, 1]
        if channel == 'basecolor':
            info = pbr.get('baseColorTexture')
            factor = list(pbr.get('baseColorFactor', factor))
        elif channel == 'normal':
            info = material.get('normalTexture')
            if not info:
                factor = [_data_factor(.5), _data_factor(.5), 1, 1]
        elif channel in ('roughness', 'metallic'):
            info = pbr.get('metallicRoughnessTexture')
            component = 'G' if channel == 'roughness' else 'B'
            scalar = float(pbr.get(channel + 'Factor', 1))
            factor = [_data_factor(scalar)] * 3 + [1]
        elif channel == 'ao':
            info, component = material.get('occlusionTexture'), 'R'
        elif channel == 'opacity':
            info, component = pbr.get('baseColorTexture'), 'A'
            scalar = float(pbr.get('baseColorFactor', [1, 1, 1, 1])[3])
            factor = [_data_factor(scalar)] * 3 + [1]
        elif channel == 'emissive':
            info = material.get('emissiveTexture')
            strength = material.get('extensions', {}).get('KHR_materials_emissive_strength', {}).get('emissiveStrength', 1)
            factor = [float(x) * strength for x in material.get('emissiveFactor', [0, 0, 0])] + [1]
        else:
            raise ValueError('该文件未提供可验证的 ' + channel + ' 通道')
        view = {'baseColorFactor': factor, 'metallicFactor': 0, 'roughnessFactor': 1}
        if info:
            selected = {k: copy.deepcopy(v) for k, v in info.items() if k in ('index', 'texCoord', 'extensions')}
            if component:
                texture = copy.deepcopy(original['textures'][info['index']])
                if 'source' not in texture:
                    raise ValueError('压缩纹理通道解码器尚未接入')
                blob = _image_bytes(original, binary, texture['source'], cache)
                with Image.open(io.BytesIO(blob)) as image:
                    image.load()
                    scalar_image = image.convert('RGBA').getchannel(component)
                    if channel in ('roughness', 'metallic', 'opacity'):
                        scalar_image = scalar_image.point(lambda pixel: round(pixel * scalar))
                    stream = io.BytesIO()
                    scalar_image.convert('RGB').save(stream, 'PNG')
                output = stream.getvalue()
                name = hashlib.sha256(output).hexdigest() + '.png'
                if not (cache / name).exists():
                    (cache / name).write_bytes(output)
                texture['source'] = len(doc.setdefault('images', []))
                doc['images'].append({'uri': name})
                selected['index'] = len(doc.setdefault('textures', []))
                doc['textures'].append(texture)
                view['baseColorFactor'] = [1, 1, 1, 1]
            view['baseColorTexture'] = selected
        name = material.get('name', '')
        sided = material.get('doubleSided', False)
        material.clear()
        material.update(name=name, doubleSided=sided, pbrMetallicRoughness=view,
                        extensions={'KHR_materials_unlit': {}})
    for key in ('extensionsUsed', 'extensionsRequired'):
        doc[key] = [x for x in doc.get(key, []) if not x.startswith('KHR_materials_')]
        doc[key].append('KHR_materials_unlit')
    encoded = json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    target = cache / ('view.' + ('glb' if binary is not None else 'gltf'))
    if binary is None:
        target.write_bytes(encoded)
    else:
        encoded += b' ' * (-len(encoded) % 4)
        binary += b'\x00' * (-len(binary) % 4)
        total = 12 + 8 + len(encoded) + 8 + len(binary)
        target.write_bytes(struct.pack('<4sII', b'glTF', 2, total)
            + struct.pack('<II', len(encoded), 0x4E4F534A) + encoded
            + struct.pack('<II', len(binary), 0x004E4942) + binary)
    return target


def obj_materials(path):
    from .importer import _mtl_file_name
    path = Path(path)
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    materials, used = {}, {line.strip()[7:].strip() for line in lines if line.strip().startswith('usemtl ')}
    for line in lines:
        if not line.strip().startswith('mtllib '):
            continue
        mtl = path.parent / line.strip()[7:].strip().strip('"')
        current = None
        for raw in mtl.read_text(encoding='utf-8-sig').splitlines():
            parts = raw.strip().split(None, 1)
            if len(parts) != 2 or parts[0].startswith('#'):
                continue
            key, value = parts
            if key == 'newmtl':
                if value in materials:
                    raise ValueError('OBJ 的材质名称冲突，不能安全生成通道预览')
                current = {};materials[value] = current
            elif current is not None:
                if key.lower().startswith('map_') or key.lower() == 'norm':
                    reference = _mtl_file_name(value)
                    target = mtl.parent / reference.replace('\\', '/')
                    if not target.is_file():
                        candidates = list(mtl.parent.rglob(Path(reference.replace('\\', '/')).name))
                        if len(candidates) == 1:
                            target = candidates[0]
                    current[key.lower()] = target
                else:
                    current[key.lower()] = value
    return lines, {name: value for name, value in materials.items() if name in used}


OBJ_SLOTS = {'normal': ('norm', 'map_kn'), 'roughness': ('map_pr', 'pr'),
             'metallic': ('map_pm', 'pm'), 'opacity': ('map_d', 'd', 'tr'),
             'emissive': ('map_ke', 'ke')}


def obj_channel(path, channel, cache):
    lines, materials = obj_materials(path)
    cache = Path(cache);cache.mkdir(parents=True, exist_ok=True)
    output = []
    for name, material in materials.items():
        key = next((k for k in OBJ_SLOTS[channel] if k in material), None)
        rgb = [.5, .5, 1.] if channel == 'normal' else [0., 0., 0.] if channel == 'emissive' else [1., 1., 1.]
        texture = None
        if key and isinstance(material[key], Path):
            texture = material[key]
            if not texture.is_file():
                raise ValueError('材质贴图缺失：' + str(texture))
        elif key:
            values = [float(v) for v in str(material[key]).split()]
            if key == 'tr':values = [1. - values[0]]
            rgb = (values * 3)[:3]
        output += ['newmtl ' + name, 'Kd ' + ' '.join(map(str, rgb if texture is None else [1, 1, 1])), 'Ke 0 0 0', 'illum 0']
        if texture:
            target = 'deps/' + hashlib.sha256(str(texture).encode()).hexdigest() + texture.suffix
            _link(texture, cache / target)
            output.append('map_Kd ' + target)
    (cache / 'channels.mtl').write_text('\n'.join(output) + '\n', encoding='utf-8')
    target = cache / 'view.obj'
    target.write_text('mtllib channels.mtl\n' + '\n'.join(line for line in lines
        if not line.strip().startswith('mtllib ')) + '\n', encoding='utf-8')
    return target


def fbx_bindings(path):
    from .importer import FBX_MAGIC, _BinaryFbxReader, _fbx_path_properties
    path = Path(path)
    with path.open('rb') as f:
        header = f.read(27)
        if not header.startswith(FBX_MAGIC):
            return {}, 'ASCII FBX 的通道绑定解析尚未接入'
        records = _BinaryFbxReader(f, path.stat().st_size, struct.unpack('<I', header[23:])[0]).read()
    objects = next((n for n in records if n.base == 'Objects'), None)
    connections = next((n for n in records if n.base == 'Connections'), None)
    if objects is None or connections is None:
        return {}, 'FBX 缺少材质连接关系'
    materials = [n for n in objects.children if n.base == 'Material']
    if len(materials) != 1:
        return {}, '多材质 FBX 的逐材质通道查看需要局部材质端口，不能用全局覆盖冒充'
    material_id = materials[0].properties[0].value
    textures = {n.properties[0].value: n for n in objects.children if n.base == 'Texture' and n.properties}
    mapping = {'DiffuseColor': 'basecolor', 'NormalMap': 'normal', 'EmissiveColor': 'emissive'}
    found = {}
    for connection in connections.children:
        values = [p.value for p in connection.properties]
        if len(values) < 4 or values[0] != 'OP' or values[2] != material_id or values[3] not in mapping:
            continue
        node = textures.get(values[1])
        if node is None:
            continue
        properties = _fbx_path_properties(node)
        reference = properties.get('RelativeFilename') or properties.get('FileName')
        if not reference:
            continue
        target = path.parent / reference.replace('\\', '/')
        if not target.is_file():
            candidates = list(path.parent.rglob(Path(reference.replace('\\', '/')).name))
            if len(candidates) != 1:
                continue
            target = candidates[0]
        slot = mapping[values[3]]
        if slot in found and found[slot] != target:
            return {}, 'FBX 同一通道存在多个纹理连接，尚未验证分层混合'
        found[slot] = target
    return found, 'FBX 未声明该通道的可验证贴图连接'


def modes_for(path, snapshot=None):
    path, snapshot = Path(path), snapshot or {}
    result = {key: '该格式未提供可验证的通道绑定' for key in CHANNELS}
    result['basecolor'] = ''  # Native unlit keeps every imported material.
    if path.suffix.lower() in ('.gltf', '.glb'):
        for key in CHANNELS[:7]:
            result[key] = ''
    elif path.suffix.lower() == '.fbx':
        found, reason = fbx_bindings(path)
        for key in CHANNELS:
            result[key] = '' if key in found else reason
        result['basecolor'] = ''
    elif path.suffix.lower() == '.obj':
        _, materials = obj_materials(path)
        for channel, keys in OBJ_SLOTS.items():
            if any(any(k in material for k in keys) for material in materials.values()):
                result[channel] = ''
        result['normal'] = result['normal'] or ''
        if result['normal']:
            result['normal'] = 'OBJ 未声明 norm/map_Kn；bump 不能冒充切线法线贴图'
    for slot, field in [('normal', 'normal_texture'), ('emissive', 'emissive_texture')]:
        if snapshot.get(field):
            result[slot] = ''
    if snapshot.get('orm_texture'):
        for key in ('roughness', 'metallic', 'ao'):
            result[key] = ''
    result['sss'] = '原生预览尚未实现 SSS 通道读取，不能用透明或透射代替'
    result['displacement'] = '原生预览尚未实现置换通道读取'
    return result


def diagnostic(path, channel, snapshot, cache):
    path = Path(path)
    reasons = modes_for(path, snapshot)
    if reasons.get(channel):
        raise ValueError(reasons[channel])
    options = {'model.unlit': True, 'model.emissive.factor': [0., 0., 0.]}
    if channel == 'basecolor':
        return path, options
    override = snapshot.get({'normal': 'normal_texture', 'emissive': 'emissive_texture',
                             'roughness': 'orm_texture', 'metallic': 'orm_texture', 'ao': 'orm_texture'}.get(channel, ''))
    if override:
        texture = Path(override)
        if channel in ('roughness', 'metallic', 'ao'):
            component = {'roughness': 'G', 'metallic': 'B', 'ao': 'R'}[channel]
            cache = Path(cache);cache.mkdir(parents=True, exist_ok=True)
            texture = cache / ('override-' + component + '.png')
            with Image.open(override) as image:
                image.convert('RGBA').getchannel(component).convert('RGB').save(texture)
    elif path.suffix.lower() in ('.gltf', '.glb'):
        return gltf_channel(path, channel, cache), options
    elif path.suffix.lower() == '.obj':
        return obj_channel(path, channel, cache), options
    else:
        texture = fbx_bindings(path)[0][channel]
    options.update({'model.color.texture': str(texture), 'model.color.rgb': [1., 1., 1.],
                    'model.color.opacity': 1.})
    return path, options
