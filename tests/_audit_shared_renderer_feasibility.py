"""Prepare read-only real inputs for the headless Babylon feasibility probe."""
import hashlib
import json
from pathlib import Path
import numpy as np

from prism.material_textures import prepare_texture, prepare_render
from prism.material_preview_profile import preview_profile
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.native.scene_data import read_parts
from prism.model_library.multimaterial.bundled_runtime import assimp_path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/shared-renderer-feasibility-20261102'
SOURCE = Path('E:/Prism-stress-100k/Game/LZPV/Assets/Envs/Slay/Assets')


def prepare_cases():
    OUT.mkdir(parents=True, exist_ok=True)
    cases = []
    for folder, stem, texture_folder, texture_stem in [
            ('SetDec/WoodCarving', 'WoodCarving', 'SetDec/WoodCarving', 'WoodCarving'),
            ('Rocks/RockWall', 'RockWall', 'Rocks/RockWall', 'RockWall'),
            ('Arch/FrontEntranceRoof/Textures', 'FrontEntranceRoof', 'SetDec/WoodCarving', 'WoodCarving')]:
        root = SOURCE / folder
        model = root / ('SM_' + stem + '.FBX')
        files = [model] + sorted(root.glob('T_*.PNG'))
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        catalog = inspect_source(model)
        parts = read_parts(model, catalog, assimp_path())
        geometry = []
        binary = OUT / (stem + '.bin')
        with binary.open('wb') as stream:
            def store(values, dtype='<f4'):
                array = np.asarray(values, dtype=dtype).reshape(-1)
                offset = stream.tell()
                stream.write(array.tobytes())
                return dict(offset=offset, count=len(array))
            for part in parts:
                assert all(len(face) == 3 for face in part['faces']), 'Probe requires triangulated source'
                geometry.append(dict(binding=part['binding_id'],
                    positions=store(part['points']),
                    normals=store(part['normals']) if part['normals'] is not None else None,
                    uv=store(part['uvs'][0]) if 0 in part['uvs'] else None,
                    indices=store(part['faces'], '<u4'),
                    matrix=part['matrix'].T.reshape(-1).tolist()))
        textures, attachments, originals = {}, {}, {}
        for slot, suffix, channel in [('basecolor','BC','RGB'), ('normal','N','RGB'),
                ('ao','ORM','R'), ('roughness','ORM','G'), ('metallic','ORM','B')]:
            path = SOURCE / texture_folder / ('T_' + texture_stem + '_' + suffix + '.PNG')
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            if suffix not in originals:
                info, blob = prepare_texture(path)
                attachment = len(originals) + 1
                originals[suffix] = (info, attachment)
                attachments[attachment] = (info['name'], info['mime'], blob)
            info, attachment = originals[suffix]
            textures[slot] = dict(info, attachmentId=attachment, channel=channel)
        payload = prepare_render(dict(id=stem, textures=textures, preview={}), attachments, OUT/stem)
        payload.update(shape='sphere', capture=False, token=stem, requestId=stem,
                       previewProfile=preview_profile())
        payload['previewProfile']['environment'] = (ROOT/'prism/assets/materials/babylon/studio.env').as_uri()
        cases.append(dict(name=stem, source=str(model), hashes=hashes, geometry=geometry,
                          geometryUrl=binary.as_uri(), geometryBytes=binary.stat().st_size,
                          texturesFrom=texture_stem,
                          slots=[dict(binding=b.binding_id, material=b.original_material_id) for b in catalog.binding_sites],
                          materials=len(catalog.definitions), payload=payload))
    (OUT/'inputs.json').write_text(json.dumps(cases), encoding='utf-8')
    print(json.dumps([dict(name=c['name'], parts=len(c['geometry']), materials=c['materials'],
                          vertices=sum(p['positions']['count']//3 for p in c['geometry'])) for c in cases]))


if __name__ == '__main__':
    prepare_cases()
