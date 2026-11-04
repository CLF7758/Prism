"""Build real inputs through the production model-to-Babylon adapters."""
import hashlib
import json
from pathlib import Path

from prism.model_library.multimaterial.browser_material import prepare_material
from prism.model_library.multimaterial.browser_scene import prepare_scene
from prism.model_library.multimaterial.bundled_runtime import assimp_path
from prism.model_library.multimaterial.catalog import catalog_to_payload
from prism.model_library.multimaterial.contracts import MaterialDefinition, TextureUse
from prism.model_library.multimaterial.inspectors import inspect_source

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/shared-renderer-integration-20261102'
OUT.mkdir(parents=True,exist_ok=True)
cases=json.loads((ROOT/'artifacts/shared-renderer-feasibility-20261102/inputs.json').read_text())
output=[]
for case in cases:
    catalog=inspect_source(case['source'])
    stem='RockWall' if case['name']=='RockWall' else 'WoodCarving'
    root=Path(case['source']).parent if case['name']!='FrontEntranceRoof' else Path(cases[0]['source']).parent
    paths={};uses=[]
    for slot,suffix,channel in [('basecolor','BC','RGB'),('normal','N','RGB'),('ao','ORM','R'),('roughness','ORM','G'),('metallic','ORM','B')]:
        path=root/f'T_{stem}_{suffix}.PNG';rid='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest();paths[rid]=str(path)
        uses.append(TextureUse(slot,rid,channel,0,'srgb' if slot=='basecolor' else 'linear',normal_convention='DirectX' if slot=='normal' else ''))
    definitions={d.material_id:MaterialDefinition(d.material_id,d.source_key,d.name,'prism-library',tuple(uses)) for d in catalog.definitions}
    artifact=dict(model_path=case['source'],catalog=catalog_to_payload(catalog),assimp_dll=str(assimp_path()),
        materials={key:prepare_material(value,paths,OUT/'materials') for key,value in definitions.items()},
        assignments={b.binding_id:b.original_material_id for b in catalog.binding_sites})
    artifact_ref=OUT/(case['name']+'.render.json')
    artifact_ref.write_text(json.dumps(artifact),encoding='utf-8')
    output.append(dict(name=case['name'],scene=prepare_scene(artifact,OUT/'geometry'),artifact_ref=str(artifact_ref),
                       ball=case['payload'],hashes=case['hashes']))
(OUT/'inputs.json').write_text(json.dumps(output),encoding='utf-8')
print(json.dumps([dict(name=c['name'],meshes=len(c['scene']['geometry']['meshes'])) for c in output]))
