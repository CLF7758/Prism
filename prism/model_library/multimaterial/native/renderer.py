"""Prism's bundled, isolated VTK renderer. Never import in the desktop process."""
import copy
import hashlib
import io
import json
import math
from collections import OrderedDict,Counter
import time
from pathlib import Path

import numpy as np
from PIL import Image
import vtk
from vtkmodules.util.numpy_support import numpy_to_vtk

# Native failures belong in the worker log, never in a hidden Win32 log window.
vtk.vtkOutputWindow.SetInstance(vtk.vtkOutputWindow())


class MaterialRenderer:
    def __init__(self, window=None, *, image_cache_bytes=192*1024*1024):
        self.window=window or vtk.vtkRenderWindow()
        if window is None:self.window.SetOffScreenRendering(True)
        self.window.SetSize(640,480);self.window.SetMultiSamples(0)
        self.window.SetAlphaBitPlanes(1)
        self.renderer=vtk.vtkRenderer();self.renderer.SetBackground(0,0,0)
        self.renderer.SetUseDepthPeeling(True)
        self.window.AddRenderer(self.renderer)
        from .studio import install_studio
        self._studio_pass = install_studio(self.renderer)
        self.actors={};self.materials={};self.assignment={};self.selected=()
        self._images=OrderedDict();self._image_bytes=0;self._uvs={};self._base_materials={}
        self._display_mode='material'
        self._active_images={};self._preparing_images=None
        self._actor_image_keys={};self._binding_image_keys=None
        self._image_cache_bytes=max(0,int(image_cache_bytes))
        self.metrics=Counter()
        self._selection_actors={}

    def render(self):
        """VTK reports shader failures as events, not Python exceptions."""
        errors=[]
        @vtk.calldata_type(vtk.VTK_STRING)
        def failed(_object, _event, message):
            if len(errors)<4:errors.append(str(message)[-1200:])
        output=vtk.vtkOutputWindow.GetInstance()
        observer=output.AddObserver('ErrorEvent',failed)
        started=time.perf_counter()
        try:
            self.window.Render()
        finally:
            output.RemoveObserver(observer)
            self.metrics['render_cpu_wait_ms']+=(time.perf_counter()-started)*1000
            self.metrics['render_calls']+=1
        if errors:raise RuntimeError('Native rendering failed: '+'; '.join(errors))

    def load(self, parts, materials, assignments):
        started=time.perf_counter()
        actors={};uvs={}
        for part in parts:
            poly=vtk.vtkPolyData();points=vtk.vtkPoints()
            values=np.asarray(part['points'],dtype='float32').reshape(-1,3)
            if not np.isfinite(values).all():raise ValueError('模型含无效顶点')
            points.SetData(numpy_to_vtk(values,deep=True));poly.SetPoints(points)
            cells=vtk.vtkCellArray()
            for face in part['faces']:
                if any(i<0 or i>=len(values) for i in face):raise ValueError('模型面索引越界')
                cells.InsertNextCell(len(face))
                for i in face:cells.InsertCellPoint(int(i))
            poly.SetPolys(cells)
            normals=part.get('normals')
            if normals is not None and len(normals)==len(values):
                array=numpy_to_vtk(np.asarray(normals,dtype='float32'),deep=True)
                array.SetName('Normals');poly.GetPointData().SetNormals(array)
            for key,data in part.get('uvs',{}).items():
                array=numpy_to_vtk(np.asarray(data,dtype='float32').reshape(-1,2),deep=True)
                array.SetName('UV'+str(key));poly.GetPointData().AddArray(array)
                if int(key)==0:poly.GetPointData().SetTCoords(array)
            if normals is None:
                normal_filter=vtk.vtkPolyDataNormals();normal_filter.SetInputData(poly)
                normal_filter.SplittingOff();normal_filter.Update();poly=normal_filter.GetOutput()
            if poly.GetPointData().GetTCoords() is not None:
                triangulate=vtk.vtkTriangleFilter();triangulate.SetInputData(poly);triangulate.Update()
                tangent_filter=vtk.vtkPolyDataTangents();tangent_filter.SetInputConnection(triangulate.GetOutputPort())
                tangent_filter.Update();poly=tangent_filter.GetOutput()
            mapper=vtk.vtkPolyDataMapper();mapper.SetInputData(poly)
            actor=vtk.vtkActor();actor.SetMapper(mapper)
            matrix=vtk.vtkMatrix4x4()
            for i in range(4):
                for j in range(4):matrix.SetElement(i,j,float(part['matrix'][i][j]))
            actor.SetUserMatrix(matrix)
            bid=part['binding_id']
            if bid in actors:raise ValueError('重复部位身份')
            actors[bid]=actor;uvs[bid]=set(int(k) for k in part.get('uvs',{}))
        self.metrics['geometry_build_ms']+=(time.perf_counter()-started)*1000
        old_actors,old_uvs=self.actors,self._uvs
        self.actors,self._uvs=actors,uvs
        try:self.apply(materials,assignments,force=True)
        except Exception:
            self.actors,self._uvs=old_actors,old_uvs
            raise
        self.renderer.RemoveAllViewProps()
        self._selection_actors.clear()
        for actor in actors.values():self.renderer.AddActor(actor)
        self.sync_highlight()
        self.renderer.ResetCamera()
        self._front_camera = self.camera_state()
        self.render()

    def camera_state(self):
        camera = self.renderer.GetActiveCamera()
        return dict(position=list(camera.GetPosition()), focal_point=list(camera.GetFocalPoint()),
                    view_up=list(camera.GetViewUp()), parallel_scale=camera.GetParallelScale(),
                    parallel_projection=bool(camera.GetParallelProjection()), view_angle=camera.GetViewAngle())

    def set_camera_state(self, state):
        camera = self.renderer.GetActiveCamera()
        camera.SetPosition(*state['position'])
        camera.SetFocalPoint(*state['focal_point'])
        camera.SetViewUp(*state['view_up'])
        camera.SetParallelScale(state['parallel_scale'])
        camera.SetParallelProjection(state['parallel_projection'])
        camera.SetViewAngle(state['view_angle'])
        self.renderer.ResetCameraClippingRange()

    def reset_front(self):
        self.set_camera_state(self._front_camera)
        self.renderer.ResetCamera()
        self.renderer.ResetCameraClippingRange()
        self.render()

    def _image(self, spec, *, alpha=False):
        path=spec.get('path')
        if not path:return None
        key=(json.dumps(spec,sort_keys=True),alpha)
        if self._binding_image_keys is not None:self._binding_image_keys.add(key)
        # Active textures are already owned by actor properties. Reuse those
        # allocations even when the optional LRU is smaller than the model's
        # texture set, and deduplicate preparation across shared-material actors.
        for images in (self._preparing_images,self._active_images):
            if images is not None and key in images:
                self.metrics['texture_reuses']+=1
                if self._preparing_images is not None:self._preparing_images[key]=images[key]
                return images[key][0]
        if key in self._images:
            self.metrics['texture_reuses']+=1
            self._images.move_to_end(key)
            if self._preparing_images is not None:self._preparing_images[key]=self._images[key]
            return self._images[key][0]
        started=time.perf_counter()
        with Image.open(path) as image:
            image.load();image=image.convert('RGBA')
            channel=spec.get('channels','RGB')
            if channel in ('R','G','B','A'):
                image=image.getchannel(channel).convert('RGB')
            elif not alpha:image=image.convert('RGB')
            if spec.get('normal_convention')=='DirectX':
                r,g,b,*rest=image.split();g=g.point(lambda x:255-x)
                image=Image.merge(image.mode,(r,g,b,*rest))
            values=np.flipud(np.asarray(image)).copy()
        self.metrics['image_read_decode_transform_ms']+=(time.perf_counter()-started)*1000
        self.metrics['image_decodes']+=1
        self.metrics['decoded_bytes']+=values.nbytes
        data=vtk.vtkImageData();data.SetDimensions(values.shape[1],values.shape[0],1)
        array=numpy_to_vtk(values.reshape(-1,values.shape[2]),deep=True,
                           array_type=vtk.VTK_UNSIGNED_CHAR)
        data.GetPointData().SetScalars(array)
        texture=vtk.vtkTexture();texture.SetInputData(data);texture.InterpolateOn();texture.MipmapOn()
        texture.SetUseSRGBColorSpace(spec.get('color_space')=='srgb')
        transform=spec.get('uv_transform') or ()
        if transform:
            x,y,angle,sx,sy=transform
            matrix=vtk.vtkMatrix4x4();matrix.Identity()
            matrix.SetElement(0,0,math.cos(angle)*sx);matrix.SetElement(0,1,-math.sin(angle)*sy)
            matrix.SetElement(1,0,math.sin(angle)*sx);matrix.SetElement(1,1,math.cos(angle)*sy)
            matrix.SetElement(0,3,x);matrix.SetElement(1,3,y)
            t=vtk.vtkTransform();t.SetMatrix(matrix);texture.SetTransform(t)
        self._images[key]=(texture,values.nbytes);self._image_bytes+=values.nbytes
        if self._preparing_images is not None:self._preparing_images[key]=self._images[key]
        while len(self._images)>128 or self._image_bytes>self._image_cache_bytes:
            _,(_,size)=self._images.popitem(last=False);self._image_bytes-=size
        return texture

    def _property(self, spec, actor, bid):
        prop=vtk.vtkProperty();prop.SetInterpolationToPBR()
        if spec.get('diagnostic'):prop.SetInterpolationToPhong()
        prop.SetColor(*spec.get('color',[.65,.65,.65])[:3])
        prop.SetMetallic(float(spec.get('metallic',0)))
        prop.SetRoughness(float(spec.get('roughness',.5)))
        prop.SetOpacity(float(spec.get('opacity',1)))
        prop.SetEmissiveFactor(*spec.get('emissive_factor',[0,0,0]))
        prop.SetNormalScale(float(spec.get('normal_scale',1)))
        prop.SetOcclusionStrength(float(spec.get('occlusion_strength',1)))
        prop.SetBackfaceCulling(not spec.get('double_sided',False))
        maps=spec.get('maps',{})
        for slot,name,setter in (
            ('basecolor','albedoTex',prop.SetBaseColorTexture),
            ('normal','normalTex',prop.SetNormalTexture),
            ('emissive','emissiveTex',prop.SetEmissiveTexture)):
            image=maps.get(slot)
            if not image or not image.get('path'):continue
            uv=int(image.get('texcoord',0))
            if uv not in self._uvs.get(bid,set()):raise ValueError('贴图要求不存在的 UV 集：'+str(uv))
            if slot=='basecolor' and spec.get('diagnostic'):
                # Scalar/normal channel views show source channel bytes. VTK
                # rejects linear maps as PBR albedo, so use a generic unlit
                # texture instead of mislabelling the map as sRGB.
                prop.SetTexture('previewTex',self._image(dict(image,color_space='linear'),alpha=True))
            else:
                expected='srgb' if slot in ('basecolor','emissive') else 'linear'
                if image.get('color_space',expected)!=expected:
                    raise ValueError(slot+' texture has an incompatible colour space')
                setter(self._image(dict(image,color_space=expected),alpha=(slot=='basecolor')))
        # ORM is explicitly packed by the preparation layer, never inferred by filename.
        if maps.get('orm',{}).get('path'):
            uv=int(maps['orm'].get('texcoord',0))
            if uv not in self._uvs.get(bid,set()):raise ValueError('ORM texture requires a missing UV set')
            prop.SetORMTexture(self._image(maps['orm']))
        if spec.get('unlit'):prop.LightingOff()
        return prop

    def _configure_uvs(self, actor, spec):
        # VTK's PBR shader samples the default tcoord varying directly. Mapping
        # every texture to a custom attribute suppresses that varying and makes
        # the generated shader fail. UV0 uses active TCoords; alternate sets
        # need both the explicit attribute and a post-generation substitution.
        mapper=actor.GetMapper();mapper.RemoveAllVertexAttributeMappings()
        shader=actor.GetShaderProperty();shader.ClearAllFragmentShaderReplacements()
        for slot,name in (('basecolor','albedoTex'),('normal','normalTex'),
                          ('emissive','emissiveTex'),('orm','materialTex')):
            if slot=='basecolor' and spec.get('diagnostic'):name='previewTex'
            image=spec.get('maps',{}).get(slot)
            if not image or not image.get('path'):continue
            uv=int(image.get('texcoord',0))
            if uv:
                mapper.MapDataArrayToMultiTextureAttribute(name,'UV'+str(uv),
                    vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS)
                shader.AddFragmentShaderReplacement(
                    'texture('+name+', tcoordVCVSOutput)',False,
                    'texture('+name+', '+name+'_coordVCVSOutput)',True)
        if spec.get('alpha_mode')=='MASK':
            cutoff=float(spec.get('alpha_cutoff',.5))
            shader.AddFragmentShaderReplacement('//VTK::Light::Impl',True,
                '//VTK::Light::Impl\n if (opacity < %.9f) { discard; }\n' % cutoff,False)

    def apply(self, materials, assignments, *, force=False):
        assignments=dict(assignments)
        prepared={}
        actor_keys={bid:set(self._actor_image_keys.get(bid,())) for bid in self.actors}
        started=time.perf_counter()
        self._preparing_images={}
        try:
            for bid,actor in self.actors.items():
                spec=materials.get(assignments.get(bid),{})
                previous=self.materials.get(self.assignment.get(bid),{})
                if not force and spec==previous and bid in self._base_materials:
                    self.metrics['properties_reused']+=1
                    continue
                self._binding_image_keys=set()
                prepared[bid]=self._property(spec,actor,bid)
                actor_keys[bid]=self._binding_image_keys
                self.metrics['properties_prepared']+=1
            needed=set().union(*actor_keys.values()) if actor_keys else set()
            active_images={key:value for key,value in {**self._active_images,**self._preparing_images}.items() if key in needed}
        finally:
            self._preparing_images=None
            self._binding_image_keys=None
            self.metrics['property_prepare_including_decode_ms']+=(time.perf_counter()-started)*1000
        # Install only after every property has been prepared successfully.
        for bid,prop in prepared.items():
            actor=self.actors[bid];actor.SetProperty(prop)
            mode=materials.get(assignments.get(bid),{}).get('alpha_mode','OPAQUE')
            actor.SetForceOpaque(mode in ('OPAQUE','MASK'));actor.SetForceTranslucent(mode=='BLEND')
        self.materials=copy.deepcopy(materials);self.assignment=assignments
        self._active_images=active_images
        self._actor_image_keys=actor_keys
        self._base_materials={bid:prepared.get(bid,self._base_materials.get(bid,actor.GetProperty())) for bid,actor in self.actors.items()}
        self.mode(self._display_mode)

    def highlight(self, binding_ids):
        self.selected=tuple(b for b in binding_ids if b in self.actors)
        self.sync_highlight()
        self.render()

    def sync_highlight(self):
        """Selection is a separate silhouette, never the material's triangles."""
        for bid in tuple(self._selection_actors):
            if bid not in self.selected or bid not in self.actors:
                self.renderer.RemoveActor(self._selection_actors.pop(bid))
        for bid in self.selected:
            source=self.actors.get(bid)
            if source is None:continue
            outline=self._selection_actors.get(bid)
            if outline is None:
                # FBX/OBJ often duplicate vertices at UV and normal seams.
                # Without welding only this derived topology, every triangle
                # is a boundary and a silhouette becomes a dense wire overlay.
                welded=vtk.vtkCleanPolyData()
                welded.SetInputData(source.GetMapper().GetInput())
                welded.PointMergingOn();welded.SetTolerance(0)
                silhouette=vtk.vtkPolyDataSilhouette()
                silhouette.SetInputConnection(welded.GetOutputPort())
                silhouette.SetCamera(self.renderer.GetActiveCamera());silhouette.SetProp3D(source)
                silhouette.SetEnableFeatureAngle(False);silhouette.BorderEdgesOn()
                mapper=vtk.vtkPolyDataMapper();mapper.SetInputConnection(silhouette.GetOutputPort())
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetRelativeCoincidentTopologyLineOffsetParameters(0,-2)
                outline=vtk.vtkActor();outline.SetMapper(mapper);outline.SetUserMatrix(source.GetMatrix())
                outline.PickableOff();outline.GetProperty().LightingOff()
                outline.GetProperty().SetColor(.22,.65,1);outline.GetProperty().SetLineWidth(2)
                self._selection_actors[bid]=outline;self.renderer.AddActor(outline)
            outline.SetVisibility(source.GetVisibility())

    def mode(self, mode):
        if mode not in ('material','material_edges','wireframe','basecolor','normal',
                        'roughness','metallic','ao','opacity','emissive'):
            raise ValueError('该预览通道尚未验证')
        self._display_mode=mode
        # Channel inspection shows data bytes, without lighting/tone mapping.
        lit = mode in ('material', 'material_edges', 'wireframe') and any(
            not spec.get('unlit', False) for spec in self.materials.values())
        self.renderer.SetPass(self._studio_pass if lit else None)
        for bid,actor in self.actors.items():
            source=self.materials.get(self.assignment.get(bid),{})
            display_spec=source
            p=self._base_materials[bid]
            if mode not in ('material','material_edges','wireframe'):
                image=source.get('maps',{}).get(mode)
                color=(source.get('color',[.65,.65,.65]) if mode=='basecolor'
                       else ([1,1,1] if image else [.5,.5,1]) if mode=='normal'
                       else source.get('emissive_factor',[0,0,0]) if mode=='emissive'
                       else [float(source.get(mode,1))]*3)
                display_spec=dict(color=color,maps={'basecolor':image} if image else {},
                    unlit=True,double_sided=True,diagnostic=True)
                p=self._property(display_spec,actor,bid)
            self._configure_uvs(actor,display_spec)
            actor.SetProperty(p)
            p.SetRepresentationToWireframe() if mode=='wireframe' else p.SetRepresentationToSurface()
            p.SetLighting(mode in ('material','material_edges','wireframe') and not source.get('unlit',False))
            p.SetEdgeVisibility(mode=='material_edges')
            if mode=='material_edges':p.SetEdgeColor(.22,.65,1.)
            # Restoring an older property can otherwise reuse uniforms from
            # the diagnostic shader: its MTime predates the last shader build.
            p.Modified();actor.GetMapper().Modified()
        self.sync_highlight()
        self.render()

    def png(self, destination, *, clean=True):
        selected=self.selected
        if clean:self.highlight(())
        self.render()
        capture=vtk.vtkWindowToImageFilter();capture.SetInput(self.window)
        capture.ReadFrontBufferOff();capture.Update()
        writer=vtk.vtkPNGWriter();writer.SetFileName(str(destination))
        writer.SetInputConnection(capture.GetOutputPort());writer.Write()
        if clean:self.highlight(selected)
        blob=Path(destination).read_bytes()
        return dict(path=str(destination),bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest())

    def unload(self):
        """Drop the model, keep the window and its OpenGL context.

        A closed preview gives its model memory back, but the window stays:
        the next preview reuses the context instead of paying for a new one.
        The caller has to read the model again afterwards, because the actors
        are gone.
        """
        self.renderer.RemoveAllViewProps()
        self.actors.clear();self.materials.clear();self.assignment.clear()
        self.selected=()
        self._images.clear();self._image_bytes=0;self._uvs.clear()
        self._base_materials.clear();self._active_images.clear()
        self._preparing_images=None;self._actor_image_keys.clear()
        self._binding_image_keys=None
        self._selection_actors.clear()

    def close(self):
        self.renderer.RemoveAllViewProps();self.actors.clear();self._images.clear()
        self._active_images.clear()
        self._selection_actors.clear()
        self.window.Finalize()

    def resource_snapshot(self):
        textures={}
        for collection in (self._images,self._active_images):
            for texture,size in collection.values():textures[texture.__this__]=size
        return dict(actors=len(self.actors),active_texture_keys=len(self._active_images),
            cached_texture_keys=len(self._images),decoded_texture_bytes=sum(textures.values()),
            optional_cache_budget_bytes=self._image_cache_bytes,
            gpu_bytes='unmeasured',basis='resident_decoded_texture_arrays; excludes geometry and GPU copies')
