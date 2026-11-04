"""Detached inputs for the shared renderer, including original-source covers."""
import hashlib
import json
from pathlib import Path

from prism.material_preview_profile import preview_profile
from .browser_geometry import prepare_geometry
from .browser_material import prepare_material
from .bundled_runtime import assimp_path
from .catalog import catalog_to_payload
from .inspectors import inspect_source
from .presentation import _publish_cache_file


def original_artifact(path, root, snapshot=None):
    from .application import ProjectMaterialService
    source = Path(path)
    catalog = inspect_source(source)
    paths = {}
    document = None
    for use in (t for d in catalog.definitions for t in d.textures):
        if not use.resource_id or use.resource_id in paths:
            continue
        candidate = source.parent / use.source_locator.replace('\\', '/')
        if candidate.is_file():
            paths[use.resource_id] = str(candidate)
        elif source.suffix.lower() in ('.gltf', '.glb'):
            from ..preview_channels import read_gltf, _image_bytes
            if document is None:document = read_gltf(source)
            doc, binary = document
            index = json.loads(use.unknown_json or '{}').get('texture_index')
            if index is not None:
                data = _image_bytes(doc, binary, doc['textures'][index]['source'], source.parent)
                digest = hashlib.sha256(data).hexdigest()
                if digest != use.resource_id.removeprefix('sha256:'):
                    raise ValueError('模型内嵌贴图内容校验失败')
                output = Path(root) / (digest + '.image')
                _publish_cache_file(output, lambda staged: staged.write_bytes(data))
                paths[use.resource_id] = str(output)
    materials = {d.material_id: prepare_material(ProjectMaterialService._normalise(d), paths, root)
                 for d in catalog.definitions}
    if snapshot and snapshot.get('has_override'):
        # Old whole-model appearance is a compatibility input, not another renderer.
        from .contracts import MaterialDefinition, TextureUse
        textures = []
        for role, field, channel in [('basecolor','color_texture','RGB'), ('normal','normal_texture','RGB'),
                ('emissive','emissive_texture','RGB'), ('ao','orm_texture','R'),
                ('roughness','orm_texture','G'), ('metallic','orm_texture','B')]:
            file = snapshot.get(field)
            if not file:continue
            digest = hashlib.sha256(Path(file).read_bytes()).hexdigest()
            rid = 'sha256:' + digest
            paths[rid] = file
            textures.append(TextureUse(role, rid, channel, 0, 'srgb' if role in ('basecolor','emissive') else 'linear',
                                       normal_convention='OpenGL' if role == 'normal' else ''))
        props = dict(normalConvention='OpenGL', slots={
            'basecolor':dict(color=snapshot.get('color_rgb') or [1,1,1]),
            'roughness':dict(value=snapshot.get('material_roughness') if snapshot.get('material_roughness') is not None else .5),
            'metallic':dict(value=snapshot.get('material_metallic') or 0),
            'normal':dict(value=snapshot.get('normal_scale') if snapshot.get('normal_scale') is not None else 1),
            'opacity':dict(value=snapshot.get('color_opacity') if snapshot.get('color_opacity') is not None else 1),
            'emissive':dict(color=snapshot.get('emissive_factor') or [0,0,0], value=1)})
        definition = MaterialDefinition('legacy', 'legacy', 'legacy', 'prism-library', tuple(textures), json.dumps(props))
        override = prepare_material(definition, paths, root)
        materials = {key: override for key in materials}
    return dict(model_path=str(source), catalog=catalog_to_payload(catalog), materials=materials,
                assignments={b.binding_id:b.original_material_id for b in catalog.binding_sites},
                assimp_dll=str(assimp_path()))


def prepare_scene(artifact, root):
    return dict(geometry=prepare_geometry(artifact, root), materials=artifact['materials'],
                assignments=artifact['assignments'], profile=preview_profile('model'))
