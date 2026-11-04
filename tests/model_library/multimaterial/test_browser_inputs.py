"""Shared material preparation and binary geometry ownership, without GUI."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
import pytest

from prism.model_library.multimaterial.browser_geometry import prepare_geometry, triangle_indices
from prism.model_library.multimaterial.browser_material import prepare_material
from prism.model_library.multimaterial.browser_scene import original_artifact, prepare_scene
from prism.model_library.multimaterial.contracts import MaterialDefinition, TextureUse
from prism.model_library.multimaterial.catalog import catalog_to_payload
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.bundled_runtime import assimp_path


def test_scalar_change_reuses_the_shared_texture_pipeline(tmp_path):
    path=tmp_path/'map.png';Image.new('RGB',(8,8),(80,120,180)).save(path)
    rid='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
    definition=MaterialDefinition('m','m','m','prism-library',
        (TextureUse('basecolor',rid,'RGB',0,'srgb'),))
    first=prepare_material(definition,{rid:str(path)},tmp_path/'cache')
    changed=replace(definition,properties_json=json.dumps({'slots':{'roughness':{'value':.19}}}))
    with patch('prism.model_library.multimaterial.browser_material.prepare_texture',side_effect=AssertionError('Repeated source decode')):
        second=prepare_material(changed,{rid:str(path)},tmp_path/'cache')
    assert first['maps']==second['maps']
    assert second['properties']['controls']['roughness']['amount']==.19
    assert second['renderer']=='babylon-shared-1'


def test_model_sss_rgb_uses_the_same_colour_preparation(tmp_path):
    path=tmp_path/'sss.png';Image.new('RGB',(8,8),(180,70,25)).save(path)
    rid='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
    definition=MaterialDefinition('m','m','m','prism-library',(TextureUse('sss',rid,'RGB',0,'linear'),))
    result=prepare_material(definition,{rid:str(path)},tmp_path/'cache')
    assert result['sssColor'] is True
    from urllib.parse import urlparse
    from urllib.request import url2pathname
    with Image.open(url2pathname(urlparse(result['maps']['sss']).path)) as image:
        assert image.getpixel((0,0)) == (180,70,25)


def test_concave_polygon_uses_ear_clipping_not_a_triangle_fan():
    points=np.array([[0,0,0],[3,0,0],[3,3,0],[1.5,1,0],[0,3,0]])
    triangles=triangle_indices(points,[[0,1,2,3,4]]).reshape(-1,3)
    assert len(triangles)==3
    area=sum(abs(np.cross(points[b]-points[a],points[c]-points[a])[2])/2 for a,b,c in triangles)
    assert area==pytest.approx(6)


def test_source_geometry_is_cached_independently_of_materials(tmp_path):
    path=tmp_path/'two.obj'
    path.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
    artifact=original_artifact(path,tmp_path/'cache')
    first=prepare_geometry(artifact,tmp_path/'geometry')
    with patch('prism.model_library.multimaterial.browser_geometry.read_parts',side_effect=AssertionError('Repeated geometry parse')):
        second=prepare_geometry(dict(artifact,materials={}),tmp_path/'geometry')
    assert first==second
    assert first['statistics']['triangles']==1
    assert Path(first['binary_path']).stat().st_size<1000


def test_bridged_hole_preserves_polygon_area_and_source_indices():
    from prism.model_library.multimaterial.native.scene_data import read_parts
    source=Path(__file__).parents[1]/'acceptance/samples/assimp_concave/concave_polygon.obj'
    part=read_parts(source,inspect_source(source),assimp_path())[0]
    points=np.asarray(part['points'])
    triangles=triangle_indices(points,part['faces']).reshape(-1,3)
    polygon=points[part['faces'][0]]
    expected=np.linalg.norm(np.cross(polygon,np.roll(polygon,-1,axis=0)).sum(axis=0))/2
    actual=np.linalg.norm(np.cross(points[triangles[:,1]]-points[triangles[:,0]],
                                  points[triangles[:,2]]-points[triangles[:,0]]),axis=1).sum()/2
    assert len(triangles)==64
    assert actual==pytest.approx(expected,rel=1e-9)
    assert triangles.max()<len(points)


def test_shared_preparation_keeps_uv_selection_and_rejects_missing_bytes(tmp_path):
    path=tmp_path/'map.png';Image.new('RGB',(8,8),(128,128,255)).save(path)
    rid='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
    definition=MaterialDefinition('m','m','m','prism-library',
        (TextureUse('normal',rid,'RGB',1,'linear',normal_convention='OpenGL'),))
    result=prepare_material(definition,{rid:str(path)},tmp_path/'cache')
    assert result['descriptors']['normal']['texcoord']==1
    assert result['properties']['normalConvention']=='OpenGL'
    path.write_bytes(b'broken')
    with pytest.raises(Exception):prepare_material(definition,{rid:str(path)},tmp_path/'cache')


def test_no_renderer_import_is_required_for_preparing_inputs(tmp_path):
    import subprocess,sys
    result=subprocess.run([sys.executable,'-c',
        "import sys; import prism.model_library.multimaterial.browser_scene; "
        "assert not any(k=='vtk' or k=='f3d' or k.startswith('vtkmodules.') for k in sys.modules)"],
        capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_surface_replacement_keeps_coverage_in_shared_browser_fields():
    from prism.model_library.multimaterial.replacement import replace_surface
    source=MaterialDefinition('source','source','source','gltf',properties_json=json.dumps({'alpha_mode':'MASK','alpha_cutoff':.37}))
    library=MaterialDefinition('library','library','library','prism-library',properties_json=json.dumps({'blendMode':'auto'}))
    result=json.loads(replace_surface(source,library).properties_json)
    assert result['blendMode']=='masked' and result['alphaCutoff']==.37
