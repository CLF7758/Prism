"""Isolated GPU checks: an acknowledged request is not proof of a new frame."""
import os
from pathlib import Path
import subprocess
import sys


def test_texture_uv_modes_and_shader_failures(tmp_path):
    script = r'''
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image
from prism.model_library.multimaterial.native.renderer import MaterialRenderer
root=Path(sys.argv[1])
image=np.zeros((16,16,3),dtype='uint8');image[:,:8]=[255,0,0];image[:,8:]=[0,255,0]
Image.fromarray(image).save(root/'map.png')
part=dict(binding_id='p',points=[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0]],
    faces=[[0,1,2],[0,2,3]],normals=[[0,0,1]]*4,matrix=np.eye(4),
    uvs={0:[[0,0],[1,0],[1,1],[0,1]],1:[[1,0],[0,0],[0,1],[1,1]]})
r=MaterialRenderer(image_cache_bytes=0)
indices=[0,1,2,0,2,3]
part=dict(part,points=[part['points'][i] for i in indices],
    faces=[[0,1,2],[3,4,5]],normals=[[0,0,1]]*6,
    uvs={key:[values[i] for i in indices] for key,values in part['uvs'].items()})
try:
    def pixels(label):
        r.png(root/(label+'.png'))
        return np.array(Image.open(root/(label+'.png')).convert('RGB'))
    r.load([part],{'m':dict(color=[1,1,1],unlit=True,double_sided=True)},{'p':'m'})
    plain=pixels('plain')
    def material(uv):
        return dict(color=[1,1,1],unlit=True,double_sided=True,maps={
            'basecolor':dict(path=str(root/'map.png'),texcoord=uv,color_space='srgb')})
    r.apply({'m':material(0)},{'p':'m'});uv0=pixels('uv0')
    assert not r._images
    from unittest.mock import patch
    with patch('PIL.Image.open',side_effect=AssertionError('Active textures must not be decoded again')):
        r.apply({'m':material(0)},{'p':'m'})
    assert np.count_nonzero(np.any(plain!=uv0,axis=2))>1000
    r.apply({'m':material(1)},{'p':'m'});uv1=pixels('uv1')
    assert np.count_nonzero(np.any(uv0!=uv1,axis=2))>1000
    r.mode('normal');pixels('normal')
    r.mode('material');assert np.array_equal(uv1,pixels('restored'))
    r.apply({'m':material(0)},{'p':'m'});assert np.array_equal(uv0,pixels('uv0again'))
    translucent=dict(material(0),alpha_mode='BLEND',opacity=.25)
    r.apply({'m':translucent},{'p':'m'});transparent=pixels('transparent')
    assert 0<float(transparent.sum())<float(uv0.sum())*.6
    emissive=dict(color=[0,0,0],emissive_factor=[1,0,0],double_sided=True,
        maps={'emissive':dict(path=str(root/'map.png'),texcoord=0,color_space='srgb')})
    r.apply({'m':emissive},{'p':'m'});emitted=pixels('emitted')
    r.apply({'m':dict(emissive,emissive_factor=[0,0,0])},{'p':'m'});no_emission=pixels('no_emission')
    assert int(emitted[:,:,0].sum())>int(no_emission[:,:,0].sum())+10000
    # ACES transforms RGB together; input emission must remain red-only.
    r.apply({'m':emissive},{'p':'m'})
    assert r._base_materials['p'].GetEmissiveFactor()==(1,0,0)
    assert r.renderer.GetPass() is r._studio_pass
    Image.new('RGB',(16,16),(128,128,255)).save(root/'normal-map.png')
    normal_material=dict(material(0),maps=dict(material(0)['maps'],normal=dict(
        path=str(root/'normal-map.png'),texcoord=0,color_space='linear')))
    r.apply({'m':normal_material},{'p':'m'});lit_before=pixels('normal_lit')
    r.mode('normal');normal_pixels=pixels('normal_map')
    assert normal_pixels[:,:,0].max()==128 and normal_pixels[:,:,2].max()==255
    r.mode('material')
    assert np.array_equal(lit_before,pixels('normal_lit_restored'))
    actor=r.actors['p']
    # Selection must not turn diagnostic/color views into dense triangle wires.
    for mode in ('material','basecolor','normal','roughness','metallic','ao','opacity','emissive'):
        r.mode(mode)
        r.highlight(('p',))
        assert not actor.GetProperty().GetEdgeVisibility(), mode
        assert actor.GetProperty().GetRepresentation()==2, mode
        assert r._selection_actors['p'].GetVisibility()
    r.mode('basecolor')
    r.png(root/'selected.png',clean=False)
    selected=np.array(Image.open(root/'selected.png').convert('RGB'))
    r.highlight(())
    r.png(root/'unselected.png',clean=False)
    unselected=np.array(Image.open(root/'unselected.png').convert('RGB'))
    # The center (including the triangulation diagonal) is untouched by outlining.
    assert np.array_equal(selected[200:280,280:360],unselected[200:280,280:360])
    assert np.count_nonzero(np.any(selected!=unselected,axis=2))>0
    r.highlight(('p',)); actor.SetVisibility(False); r.sync_highlight()
    assert not r._selection_actors['p'].GetVisibility()
    actor.SetVisibility(True); r.sync_highlight()
    assert r._selection_actors['p'].GetVisibility()
    r.mode('material_edges'); assert actor.GetProperty().GetEdgeVisibility()
    r.mode('wireframe'); assert actor.GetProperty().GetRepresentation()==1
    r.mode('material'); assert not actor.GetProperty().GetEdgeVisibility()
    r.highlight(())
    actor.GetShaderProperty().AddFragmentShaderReplacement('//VTK::Light::Impl',True,
        '//VTK::Light::Impl\n gl_FragData[0]=DOES_NOT_EXIST;\n',False)
    try:r.render()
    except RuntimeError as error:assert 'Native rendering failed' in str(error)
    else:raise AssertionError('A shader compilation failure was reported as success')
finally:r.close()
'''
    environment=dict(os.environ,QT_QPA_PLATFORM='offscreen')
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],
        cwd=Path(__file__).resolve().parents[3],env=environment,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr


def test_roughness_update_keeps_geometry_camera_and_unaffected_material(tmp_path):
    script = r"""
import copy, numpy as np
from prism.model_library.multimaterial.native.renderer import MaterialRenderer
r=MaterialRenderer()
part=dict(points=[[-1,-1,0],[1,-1,0],[0,1,0]],faces=[[0,1,2]],
          normals=[[0,0,1]]*3,matrix=np.eye(4),uvs={})
materials={'a':dict(color=[1,0,0],roughness=.2),'b':dict(color=[0,1,0],roughness=.4)}
assignments={'left':'a','right':'b'}
try:
    r.load([dict(part,binding_id='left'),dict(part,binding_id='right')],materials,assignments)
    geometry={bid:actor.GetMapper().GetInput().__this__ for bid,actor in r.actors.items()}
    camera=r.camera_state()
    before=r.metrics['properties_prepared']
    right=r._base_materials['right']
    # Mutate caller dictionary too: renderer must own its previous-state snapshot.
    materials['a']['roughness']=.7
    r.apply(materials,assignments)
    assert r.metrics['properties_prepared']==before+1
    assert r._base_materials['right'] is right
    assert abs(r._base_materials['left'].GetRoughness()-.7)<1e-6
    assert r.camera_state()==camera
    assert {bid:actor.GetMapper().GetInput().__this__ for bid,actor in r.actors.items()}==geometry
    before=r.metrics['properties_prepared']
    r.apply(materials,assignments)
    assert r.metrics['properties_prepared']==before
    assert r.metrics['properties_reused']>=3
finally:r.close()
"""
    environment=dict(os.environ,QT_QPA_PLATFORM='offscreen')
    result=subprocess.run([sys.executable,'-c',script],cwd=Path(__file__).resolve().parents[3],
                          env=environment,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
