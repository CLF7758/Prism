"""Model definitions adapted to the material library's exact texture pipeline."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlparse
from urllib.request import url2pathname

from prism.material_parameters import normal_convention, slot_options
from prism.material_preview_profile import preview_profile
from prism.material_textures import prepare_render, prepare_texture, payload_for_record, texture_signature
from .vocabulary import STANDARD_SLOTS

RENDERER_VERSION = 'babylon-shared-1'


def prepare_material(definition, paths, root):
    properties = json.loads(definition.properties_json or '{}')
    # Source formats spell these fields differently; their meaning stays shared.
    properties.setdefault('blendMode', {'OPAQUE': 'opaque', 'MASK': 'masked', 'BLEND': 'translucent'}.get(
        properties.get('alpha_mode'), 'auto'))
    properties.setdefault('alphaCutoff', properties.get('alpha_cutoff', .5))
    slots = properties.setdefault('slots', {})
    if 'emissive' not in slots and properties.get('emissive_factor') is not None:
        slots['emissive'] = dict(color=properties['emissive_factor'], value=1)
    if 'normal' not in slots and 'normal_scale' in properties:
        slots['normal'] = dict(value=properties['normal_scale'])
    if 'ao' not in slots and 'occlusion_strength' in properties:
        slots['ao'] = dict(value=properties['occlusion_strength'])
    textures, descriptors, source_files = {}, {}, {}
    for use in definition.textures:
        if use.slot not in STANDARD_SLOTS or not slot_options(properties, use.slot, True)['enabled']:
            continue
        path = paths.get(use.resource_id)
        if not path:
            continue  # The source catalog already reports unresolved resources.
        path = Path(path)
        stat = path.stat()
        source_files[use.resource_id] = (path, stat.st_size, stat.st_mtime_ns)
        textures[use.slot] = dict(name=path.name, sha256=use.resource_id.removeprefix('sha256:'),
                                  attachmentId=use.resource_id, channel=use.channels)
        descriptors[use.slot] = dict(texcoord=use.texcoord, uv_transform=list(use.uv_transform),
            color_space=use.color_space)
        if use.slot == 'normal':
            properties['normalConvention'] = normal_convention(properties, use.normal_convention)
    # Babylon's combined MR input cannot represent two different UV transforms.
    mr = [descriptors[k] for k in ('roughness', 'metallic') if k in descriptors]
    if len(mr) == 2 and any(mr[0].get(k) != mr[1].get(k) for k in ('texcoord', 'uv_transform')):
        raise ValueError('粗糙度与金属度使用不同 UV，无法安全合并预览')
    record = dict(id=definition.material_id, textures=textures, preview=dict(properties=properties))
    key = hashlib.sha256(json.dumps([RENDERER_VERSION, texture_signature(record),
        [(rid, str(p), size, modified) for rid, (p, size, modified) in sorted(source_files.items())]],
        sort_keys=True).encode()).hexdigest()
    directory = Path(root) / key
    manifest = directory / 'material.json'
    if manifest.is_file():
        payload = payload_for_record(json.loads(manifest.read_text(encoding='utf-8')), record)
        payload.update(descriptors=descriptors, renderer=RENDERER_VERSION, previewProfile=preview_profile('model'))
        if mr:payload['descriptors']['metallicRoughness'] = mr[0]
        return payload
    attachments = {}
    for rid, (path, _, _) in source_files.items():
        info, blob = prepare_texture(path)
        if info['sha256'] != rid.removeprefix('sha256:'):
            raise ValueError('模型贴图内容与已登记资源不一致')
        attachments[rid] = (info['name'], info['mime'], blob)
        for binding in textures.values():
            if binding['attachmentId'] == rid:binding.update(info)
    from .presentation import _publish_cache_file
    Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='material-stage-', dir=root) as staging:
        payload = prepare_render(record, attachments, Path(staging).resolve())
        for slot, uri in list(payload['maps'].items()):
            parsed = urlparse(uri)
            source = Path(url2pathname(('//'+parsed.netloc if parsed.netloc else '')+parsed.path))
            destination = directory / source.name
            _publish_cache_file(destination, lambda staged, source=source: shutil.copyfile(source, staged))
            payload['maps'][slot] = destination.resolve().as_uri()
    if mr:
        descriptors['metallicRoughness'] = mr[0]
    payload.update(descriptors=descriptors, renderer=RENDERER_VERSION, previewProfile=preview_profile('model'))
    _publish_cache_file(manifest, lambda staged: staged.write_text(json.dumps(payload), encoding='utf-8'))
    return payload
