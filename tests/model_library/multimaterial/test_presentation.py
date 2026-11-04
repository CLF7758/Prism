import hashlib
import json

from PIL import Image

from prism.model_library.multimaterial.contracts import MaterialDefinition, TextureUse
from prism.model_library.multimaterial.presentation import material_spec


def test_source_factors_and_specified_defaults_survive_normalisation():
    from prism.model_library.multimaterial.application import ProjectMaterialService
    obj=MaterialDefinition('red','source','Red','mtl_illum_2',properties_json=json.dumps({
        'kd_color':[1,0,0],'opacity':[.5],'roughness_factor':.31}))
    spec=material_spec(ProjectMaterialService._normalise(obj),{})
    assert spec['color']==[1,0,0]
    assert spec['alpha_mode']=='BLEND'
    assert spec['roughness']==.31
    gltf=MaterialDefinition('default','source','Default','gltf_metallic_roughness')
    spec=material_spec(ProjectMaterialService._normalise(gltf),{})
    assert spec['color']==[1,1,1]
    assert spec['metallic']==1


def test_independent_channels_are_packed_without_changing_originals(tmp_path):
    uses=[];paths={};hashes={}
    for slot,value,channel in [('ao',90,'R'),('roughness',60,'G'),('metallic',30,'B')]:
        path=tmp_path/(slot+'.png');Image.new('RGB',(2,2),(value,value,value)).save(path)
        rid='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
        paths[rid]=str(path);hashes[path]=hashlib.sha256(path.read_bytes()).hexdigest()
        uses.append(TextureUse(slot,rid,channel,0,'linear'))
    definition=MaterialDefinition('m','source','Test','pbr',tuple(uses))
    spec=material_spec(definition,paths,tmp_path)
    with Image.open(spec['maps']['orm']['path']) as image:
        assert image.getpixel((0,0))==(90,60,30)
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==digest for path,digest in hashes.items())


def test_alpha_binding_and_base_alpha_multiply_without_touching_source(tmp_path):
    base=tmp_path/'base.png';mask=tmp_path/'mask.png'
    Image.new('RGBA',(2,2),(100,120,140,128)).save(base)
    Image.new('L',(2,2),128).save(mask)
    definition=MaterialDefinition('m','source','Test','pbr',(
        TextureUse('basecolor','base','RGB',0,'srgb'),
        TextureUse('opacity','mask','R',0,'linear')),json.dumps({'alpha_mode':'BLEND'}))
    spec=material_spec(definition,{'base':str(base),'mask':str(mask)},tmp_path)
    with Image.open(spec['maps']['basecolor']['path']) as image:
        assert image.getpixel((0,0))==(100,120,140,64)
    with Image.open(base) as image:assert image.getpixel((0,0))[-1]==128


def test_scalar_resolution_is_detached_and_does_not_mutate_input():
    from copy import deepcopy
    from prism.model_library.multimaterial.presentation import material_parameters
    properties = {'slots': {'roughness': {'value': .2, 'factor': .7, 'invert': True},
                            'emissive': {'enabled': False, 'color': [1, .5, 0]}}}
    original = deepcopy(properties)
    assert material_parameters(properties)['roughness'] == .8
    mapped = material_parameters(properties, {'roughness'})
    assert mapped['roughness'] == .7
    assert mapped['emissive_factor'] == [0, 0, 0]
    assert 'maps' not in mapped
    assert properties == original
