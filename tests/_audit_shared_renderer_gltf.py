"""Generate a glTF texture/normal/UV-transform reference through production inputs."""
import json
from pathlib import Path
import numpy as np
from PIL import Image
from prism.model_library.multimaterial.browser_scene import original_artifact,prepare_scene

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/shared-renderer-integration-20261102/gltf-reference'
OUT.mkdir(parents=True,exist_ok=True)
image=np.zeros((32,32,3),dtype='uint8')
image[:16,:16]=[220,30,20];image[:16,16:]=[20,200,30]
image[16:,:16]=[30,40,210];image[16:,16:]=[220,180,20]
Image.fromarray(image).save(OUT/'checker.png')
Image.new('RGB',(32,32),(180,100,235)).save(OUT/'normal.png')
arrays=[np.array([[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0]],dtype='<f4'),
        np.array([[0,0,1]]*4,dtype='<f4'),np.array([[0,1],[1,1],[1,0],[0,0]],dtype='<f4'),
        np.array([0,1,2,0,2,3],dtype='<u2')]
views=[];offset=0;blob=b''
for array in arrays:
    data=array.tobytes();views.append(dict(buffer=0,byteOffset=offset,byteLength=len(data)));offset+=len(data);blob+=data
(OUT/'model.bin').write_bytes(blob)
document=dict(asset={'version':'2.0'},scene=0,scenes=[dict(nodes=[0])],nodes=[dict(mesh=0)],
    buffers=[dict(uri='model.bin',byteLength=len(blob))],bufferViews=views,
    accessors=[dict(bufferView=i,componentType=5123 if i==3 else 5126,count=6 if i==3 else 4,
        type=['VEC3','VEC3','VEC2','SCALAR'][i],**(dict(min=[-1,-1,0],max=[1,1,0]) if i==0 else {})) for i in range(4)],
    meshes=[dict(primitives=[dict(attributes=dict(POSITION=0,NORMAL=1,TEXCOORD_0=2),indices=3,material=0)])],
    images=[dict(uri='checker.png'),dict(uri='normal.png')],textures=[dict(source=0),dict(source=1)],
    materials=[dict(doubleSided=True,pbrMetallicRoughness=dict(baseColorTexture=dict(index=0,
        extensions={'KHR_texture_transform':dict(offset=[-.15,.12],scale=[.8,.7],rotation=.25)}),metallicFactor=0,roughnessFactor=1),
        normalTexture=dict(index=1))],extensionsUsed=['KHR_texture_transform'])
source=OUT/'reference.gltf';source.write_text(json.dumps(document),encoding='utf-8')
scene=prepare_scene(original_artifact(source,OUT/'cache'),OUT/'geometry')
(OUT/'input.json').write_text(json.dumps(dict(source=source.as_uri(),scene=scene)),encoding='utf-8')
print('glTF reference input ready')
