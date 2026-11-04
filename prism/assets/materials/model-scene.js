/* Model geometry adapter. Engine, studio, texture loader and PBR settings belong to render.js. */
'use strict';
window.prismModel=(()=>{
  let revision=0,root=null,geometryKey='',meshes=new Map(),materials=new Map(),specs={},assignments={};
  let textureCache=new Map(),neededTextures=new Set(),displayMode='material',selection=[],lod=undefined;
  let outlineTimer=null,edges=[];
  const preparingTextures=new Map();
  const fields={0:'uvs',1:'uvs2',2:'uvs3',3:'uvs4',4:'uvs5',5:'uvs6'};
  const json=value=>JSON.stringify(value);
  const event=(name,data)=>bridge.model_event?.(name,json(data));
  function clearSelection(){
    clearTimeout(outlineTimer);outlineTimer=null;selection=[];
    for(const mesh of meshes.values())mesh.renderOutline=false;
    wake();
  }
  function disposeEdges(){for(const mesh of edges)mesh.dispose();edges=[];}
  function release(){
    revision++;clearSelection();disposeEdges();
    for(const mesh of meshes.values())mesh.dispose();meshes.clear();root?.dispose();root=null;
    for(const entry of materials.values())disposeMaterial(entry);materials.clear();
    preparingTextures.clear();
    for(const entry of textureCache.values())entry.promise.then(value=>value.dispose()).catch(()=>{});textureCache.clear();
    neededTextures.clear();geometryKey='';specs={};assignments={};
    displayMode='material';lod=undefined;
    engine?.stopRenderLoop(renderFrame);
  }
  function bounds(){
    let min=new B.Vector3(Infinity,Infinity,Infinity),max=new B.Vector3(-Infinity,-Infinity,-Infinity),count=0;
    for(const mesh of meshes.values())if(mesh.isEnabled()){
      mesh.computeWorldMatrix(true);const box=mesh.getBoundingInfo().boundingBox;
      min=B.Vector3.Minimize(min,box.minimumWorld);max=B.Vector3.Maximize(max,box.maximumWorld);count++;
    }
    if(!count)throw new Error('当前 LOD 没有可见模型部位');
    return {min,max};
  }
  function cameraState(){
    camera.getViewMatrix(true);
    return {renderer:'babylon',version:1,position:camera.position.asArray(),focal_point:camera.target.asArray(),
      view_up:camera.upVector.asArray(),alpha:camera.alpha,beta:camera.beta,radius:camera.radius,
      view_angle:camera.fov*180/Math.PI,parallel_projection:camera.mode===B.Camera.ORTHOGRAPHIC_CAMERA,
      parallel_scale:camera.orthoTop||1};
  }
  function restoreCamera(value){
    if(!value)return;
    const point=v=>B.Vector3.FromArray(value.renderer==='babylon'?v:[v[0],v[1],-v[2]]);
    if(value.position&&value.focal_point){camera.target=point(value.focal_point);camera.setPosition(point(value.position));}
    if(value.renderer==='babylon'&&Number.isFinite(value.alpha)&&Number.isFinite(value.beta)&&Number.isFinite(value.radius)){
      camera.alpha=value.alpha;camera.beta=value.beta;camera.radius=value.radius;
    }
    camera.upVector=value.view_up?point(value.view_up):B.Vector3.Up();
    if(Number.isFinite(value.view_angle))camera.fov=value.view_angle*Math.PI/180;
    camera.mode=value.parallel_projection?B.Camera.ORTHOGRAPHIC_CAMERA:B.Camera.PERSPECTIVE_CAMERA;
    const scale=Number(value.parallel_scale)||1,aspect=engine.getAspectRatio(camera);
    camera.orthoTop=scale;camera.orthoBottom=-scale;camera.orthoLeft=-scale*aspect;camera.orthoRight=scale*aspect;
    camera.inertialAlphaOffset=0;camera.inertialBetaOffset=0;camera.inertialRadiusOffset=0;
    camera.inertialPanningX=0;camera.inertialPanningY=0;wake();
  }
  function fit(front=true){
    if(front)camera.fov=.8;
    const box=bounds(),size=box.max.subtract(box.min),radius=Math.max(size.length()/2,1e-5);
    camera.mode=B.Camera.PERSPECTIVE_CAMERA;camera.upVector=B.Vector3.Up();camera.target=box.min.add(box.max).scale(.5);
    const aspect=engine.getAspectRatio(camera),angle=Math.min(camera.fov/2,Math.atan(Math.tan(camera.fov/2)*aspect));
    camera.radius=radius/Math.sin(angle)*1.12;camera.minZ=Math.max(radius/10000,1e-7);camera.maxZ=Math.max(camera.radius*100,1);
    camera.lowerRadiusLimit=radius*.01;camera.upperRadiusLimit=radius*1000;
    camera.panningSensibility=1000/Math.max(radius,.001);camera.wheelDeltaPercentage=.01;
    if(front){camera.alpha=-Math.PI/2;camera.beta=Math.PI/2;}
    camera.inertialAlphaOffset=camera.inertialBetaOffset=camera.inertialRadiusOffset=0;
    camera.inertialPanningX=camera.inertialPanningY=0;
    wake();
  }
  function setLod(value,refit=true){
    lod=value;for(const mesh of meshes.values())mesh.setEnabled(value==null||mesh.metadata.lod==null||mesh.metadata.lod===value);
    clearSelection();mode(displayMode);if(refit)fit(false);
    return [...meshes.values()].filter(m=>m.isEnabled()).length;
  }
  function highlight(ids,deadline){
    clearSelection();selection=ids.filter(id=>meshes.get(id)?.isEnabled());
    for(const id of selection){const mesh=meshes.get(id);mesh.renderOutline=true;mesh.outlineColor=new B.Color3(.25,.6,1);mesh.outlineWidth=Math.max(camera.radius*.002,1e-5);}
    const delay=deadline==null?1200:Math.max(0,Math.min(1200,deadline));
    outlineTimer=setTimeout(clearSelection,delay);wake();return selection.length;
  }
  function debugMaterial(entry,slot){
    const source=entry.textures[slot];if(!source)return entry.material;
    if(!entry.debug.has(slot)){
      const material=new B.PBRMaterial('channel:'+slot,scene),map=source.clone();map.gammaSpace=true;
      material.albedoTexture=map;material.albedoColor=B.Color3.White();material.unlit=true;material.backFaceCulling=false;
      material.imageProcessingConfiguration=new B.ImageProcessingConfiguration();material.imageProcessingConfiguration.toneMappingEnabled=false;
      entry.debug.set(slot,{material,map});
    }
    return entry.debug.get(slot).material;
  }
  function disposeMaterial(entry){
    for(const value of entry.debug.values()){value.material.dispose(false,false);value.map.dispose();}
    entry.material.dispose(false,false);
  }
  function mode(value){
    displayMode=value;disposeEdges();
    status.textContent=!['material','material_edges','wireframe'].includes(value)
      &&![...materials.values()].some(entry=>entry.textures[value])?'该槽位未添加贴图':'';
    for(const [id,mesh] of meshes){
      const entry=materials.get(assignments[id]);if(!entry)continue;
      entry.material.wireframe=value==='wireframe';
      mesh.material=['material','material_edges','wireframe'].includes(value)?entry.material:debugMaterial(entry,value);
      if(value==='material_edges'&&mesh.isEnabled()){
        const edge=mesh.clone(id+':edges',mesh.parent,true);edge.isPickable=false;edge.renderOutline=false;
        const wire=new B.StandardMaterial('edges',scene);wire.disableLighting=true;wire.emissiveColor=new B.Color3(.08,.1,.14);wire.wireframe=true;
        wire.zOffset=-1;edge.material=wire;edge.onDisposeObservable.addOnce(()=>wire.dispose());edges.push(edge);
      }
    }
    wake();
  }
  async function binary(url){
    return new Promise((resolve,reject)=>{const request=new XMLHttpRequest();request.open('GET',url);request.responseType='arraybuffer';
      request.onload=()=>request.response?resolve(request.response):reject(new Error('几何数据为空'));
      request.onerror=()=>reject(new Error('读取模型几何失败'));request.send();});
  }
  async function loadGeometry(input,ticket){
    const bytes=await binary(input.binary_url);if(ticket!==revision)return null;
    const result=new Map(),parent=new B.Mesh('model-root',scene);parent.scaling.z=-1;parent.setEnabled(false);
    try{
      const floats=field=>new Float32Array(bytes,field.offset,field.count);
      for(const part of input.meshes){
        const mesh=new B.Mesh(part.binding_id,scene);mesh.parent=parent;result.set(part.binding_id,mesh);
        if(input.uv_origin==='top-left')mesh.overrideMaterialSideOrientation=B.Material.ClockWiseSideOrientation;
        const data=new B.VertexData();data.positions=floats(part.positions);data.indices=new Uint32Array(bytes,part.indices.offset,part.indices.count);
        data.normals=part.normals?floats(part.normals):[];
        if(!part.normals)B.VertexData.ComputeNormals(data.positions,data.indices,data.normals);
        for(const [key,field] of Object.entries(part.uvs)){if(!fields[key])throw new Error('暂不支持 UV 集合 '+key);data[fields[key]]=floats(field);}
        data.applyToMesh(mesh,true);mesh.setPreTransformMatrix(B.Matrix.FromArray(part.matrix));
        mesh.metadata={binding:part.binding_id,lod:part.lod,displacement:'',displacementBase:null};
      }
      return {meshes:result,root:parent};
    }catch(error){for(const mesh of result.values())mesh.dispose();parent.dispose();throw error;}
  }
  async function prepareMaterials(input,ticket,keep){
    const prepared=new Map();
    try{
      for(const [id,spec] of Object.entries(input)){
        const signature=json(spec),old=materials.get(id);
        const textures={};
        for(const [slot,url] of Object.entries(spec.maps||{})){
          const descriptor=spec.descriptors?.[slot]||{},key=json([url,slot==='basecolor'||slot==='emissive',descriptor]);keep.add(key);
          if(!textureCache.has(key)){
            const entry={texture:null,promise:null};textureCache.set(key,entry);
            entry.promise=texture(url,['srgb','linear'].includes(descriptor.color_space)?descriptor.color_space==='srgb':['basecolor','emissive'].includes(slot),descriptor.uv_origin!=='top-left').then(value=>{
              entry.texture=value;value.coordinatesIndex=descriptor.texcoord||0;
              const uv=descriptor.uv_transform;if(uv?.length){value.uOffset=uv[0];value.vOffset=uv[1];value.wAng=(descriptor.uv_origin==='top-left'?-1:1)*uv[2];value.uScale=uv[3];value.vScale=uv[4];value.uRotationCenter=0;value.vRotationCenter=0;}
              return value;
            }).catch(error=>{if(textureCache.get(key)===entry)textureCache.delete(key);throw error;});
          }
          textures[slot]=await textureCache.get(key).promise;
        }
        if(ticket!==revision)throw new Error('预览请求已过期');
        if(old?.signature===signature){prepared.set(id,old);continue;}
        const material=new B.PBRMaterial('slot:'+id,scene);applySettings(spec,material,textures);
        material.backFaceCulling=spec.properties.double_sided===false;material.unlit=!!spec.properties.unlit;
        prepared.set(id,{material,textures,signature,debug:new Map(),spec});
      }
      return prepared;
    }catch(error){for(const [id,entry] of prepared)if(entry!==materials.get(id))disposeMaterial(entry);throw error;}
  }
  function pruneTextures(keep){
    keep=new Set(keep);for(const values of preparingTextures.values())for(const key of values)keep.add(key);
    for(const [key,entry] of textureCache)if(!keep.has(key)){
      textureCache.delete(key);entry.promise.then(value=>value.dispose()).catch(()=>{});
    }
  }
  async function compileVisible(){
    const compiled=new Set();
    for(const mesh of meshes.values())if(mesh.isEnabled()){
      const key=mesh.material.uniqueId+':'+mesh.getVerticesDataKinds().sort().join(',');
      if(!compiled.has(key)){compiled.add(key);await mesh.material.forceCompilationAsync(mesh);}
    }
  }
  async function load(data){
    if(sphere||modelContainer||material)releaseMaterial(true);
    const ticket=++revision;await init(data.profile);if(ticket!==revision)return {stale:true};
    const keep=new Set();preparingTextures.set(ticket,keep);let geometry=null,prepared=null;
    try{
      const changed=geometryKey!==data.geometry.key;
      if(changed)geometry=await loadGeometry(data.geometry,ticket);
      const origin=data.geometry.uv_origin||'bottom-left';
      const inputs=Object.fromEntries(Object.entries(data.materials).map(([id,spec])=>[id,{...spec,
        geometryUVOrigin:origin,previewProfile:data.profile,
        descriptors:Object.fromEntries(Object.keys(spec.maps||{}).map(slot=>[slot,{...spec.descriptors?.[slot],uv_origin:origin}]))}]));
      prepared=await prepareMaterials(inputs,ticket,keep);
      if(ticket!==revision)throw new Error('预览请求已过期');
      const nextMeshes=geometry?.meshes||meshes;
      for(const [id,mesh] of nextMeshes){
        if(!prepared.has(data.assignments[id]))throw new Error('模型部位缺少材质槽：'+id);
        const spec=prepared.get(data.assignments[id]).spec;
        for(const slot of Object.keys(spec.maps||{})){
          const uv=spec.descriptors?.[slot]?.texcoord||0;
          if(!mesh.isVerticesDataPresent(B.VertexBuffer[uv?'UV'+(uv+1)+'Kind':'UVKind']))throw new Error('贴图要求不存在的 UV 集合 '+uv);
          if(slot==='displacement'&&(uv!==0||(spec.descriptors?.[slot]?.uv_transform||[]).length))
            throw new Error('置换贴图可保存；当前预览只支持 UV0 且不含独立 UV 变换');
        }
      }
      clearSelection();disposeEdges();
      if(geometry){for(const mesh of meshes.values())mesh.dispose();root?.dispose();meshes=geometry.meshes;root=geometry.root;geometryKey=data.geometry.key;root.setEnabled(true);geometry=null;}
      for(const [id,entry] of materials)if(entry!==prepared.get(id))disposeMaterial(entry);
      materials=prepared;prepared=null;assignments={...data.assignments};specs=inputs;neededTextures=keep;pruneTextures(keep);
      if(changed){const levels=[...new Set(data.geometry.meshes.map(m=>m.lod).filter(v=>v!=null))].sort((a,b)=>a-b);lod=levels[0];setLod(lod,false);fit(true);}
      mode(displayMode);
      const displacementInputs=new Map();
      for(const [id,mesh] of meshes){
        const spec=specs[assignments[id]],url=spec.maps?.displacement;
        const key=json([url,spec.properties.controls?.displacement]);
        if(mesh.metadata.displacement!==key){
          if(url&&!mesh.metadata.displacementBase)mesh.metadata.displacementBase={
            positions:new Float32Array(mesh.getVerticesData(B.VertexBuffer.PositionKind)),
            normals:new Float32Array(mesh.getVerticesData(B.VertexBuffer.NormalKind))};
          const base=mesh.metadata.displacementBase;
          if(base){mesh.updateVerticesData(B.VertexBuffer.PositionKind,base.positions.slice());mesh.updateVerticesData(B.VertexBuffer.NormalKind,base.normals.slice());}
          if(url){
            if(!displacementInputs.has(url))displacementInputs.set(url,displacementPixels(url,()=>ticket===revision));
            await displacement(url,mesh,spec.properties.controls?.displacement,()=>ticket===revision,await displacementInputs.get(url));
          }else mesh.metadata.displacementBase=null;
          mesh.metadata.displacement=key;mesh.refreshBoundingInfo();
        }
      }
      if(ticket!==revision)return {stale:true};
      await compileVisible();await scene.whenReadyAsync();if(ticket!==revision)return {stale:true};
      scene.render();wake();return snapshot();
    }catch(error){
      if(geometry){for(const mesh of geometry.meshes.values())mesh.dispose();geometry.root.dispose();}
      if(prepared)for(const [id,entry] of prepared)if(entry!==materials.get(id))disposeMaterial(entry);
      throw error;
    }finally{preparingTextures.delete(ticket);pruneTextures(neededTextures);}
  }
  function snapshot(){return {bindings:[...meshes.keys()],highlight_binding_count:selection.length,
    camera:cameraState(),lod_levels:[...new Set([...meshes.values()].map(m=>m.metadata.lod).filter(v=>v!=null))].sort((a,b)=>a-b),
    resources:{actors:meshes.size,materials:materials.size,textures:textureCache.size},gpu:engine.getGlInfo(),webgl:engine.webGLVersion};}
  async function capture(data){
    const selected=[...selection],oldMode=displayMode,oldCamera=cameraState();clearSelection();mode('material');
    restoreCamera(data.camera||oldCamera);
    try{
      // Read the existing preserved framebuffer. A second render target can
      // compile another PBR pass and duplicate GPU allocations for large models.
      if(engine.isDisposed||engine._gl?.isContextLost())throw new Error('图形设备已断开，无法生成封面');
      await compileVisible();await scene.whenReadyAsync();scene.render();
      const output=document.createElement('canvas');
      const size=value=>Math.max(1,Math.min(1024,Math.floor(Number(value)||512)));
      output.width=size(data.width);output.height=size(data.height);
      const context=output.getContext('2d',{willReadFrequently:true});
      if(!context||!canvas.width||!canvas.height)throw new Error('无法读取当前预览画面');
      context.fillStyle='#000';context.fillRect(0,0,output.width,output.height);
      const scale=Math.min(output.width/canvas.width,output.height/canvas.height);
      const width=canvas.width*scale,height=canvas.height*scale;
      context.drawImage(canvas,(output.width-width)/2,(output.height-height)/2,width,height);
      return {image:output.toDataURL('image/png'),camera:cameraState()};
    }finally{
      if(data.release_scene)release();
      else{restoreCamera(oldCamera);mode(oldMode);if(selected.length)highlight(selected);}
    }
  }
  async function thumbnail(data){
    await init(data.profile);const spec=data.material,keep=new Set();
    const entries=await prepareMaterials({thumbnail:{...spec,descriptors:{},previewProfile:data.profile}},revision,keep);
    const entry=entries.get('thumbnail');
    try{await scene.whenReadyAsync();return {image:await materialBallImage(entry.material,128)};}
    finally{if(materials.get('thumbnail')!==entry)disposeMaterial(entry);pruneTextures(neededTextures);}
  }
  async function execute(action,data){
    if(action==='init'){await init(data.profile);await scene.whenReadyAsync();scene.render();return {gpu:engine.getGlInfo(),webgl:engine.webGLVersion};}
    if(action==='load'||action==='open')return load(data);
    if(action==='thumbnail')return thumbnail(data);
    if(action==='release'){release();return {};}
    if(action==='cancel'){revision++;clearSelection();return {};}
    if(graphicsLost)throw new Error('图形设备已断开，请关闭并重新打开预览');
    if(!root)throw new Error('尚未加载模型');
    if(action==='mode'){clearSelection();mode(data.mode);}
    else if(action==='highlight')highlight(data.binding_ids||[],data.duration_ms);
    else if(action==='reset'){clearSelection();fit(true);}
    else if(action==='lod')setLod(data.level);
    else if(action==='camera')restoreCamera(data.camera);
    else if(action==='cover')return capture(data);
    else if(action!=='state')throw new Error('未知预览操作：'+action);
    await compileVisible();await scene.whenReadyAsync();scene.render();return snapshot();
  }
  canvas.addEventListener('pointerdown',pointer=>{
    if(!root)return;clearSelection();
    if(pointer.ctrlKey){const rect=canvas.getBoundingClientRect();
      const hit=scene.pick(pointer.clientX-rect.left,pointer.clientY-rect.top,m=>meshes.has(m.name));
      if(hit?.hit)event('material.picked',{binding_id:hit.pickedMesh.name});}
  });
  return {get active(){return !!root},release,load,execute,snapshot,cameraState,async command(id,action,data){
    try{bridge.model_result(id,json({ok:true,...await execute(action,data)}));}
    catch(error){bridge.model_result(id,json({ok:false,error:String(error.message||error)}));}
  }};
})();
