/* Babylon.js supplies PBR lighting, subsurface transport and mesh displacement. */
'use strict';
let bridge, engine, scene, sphere, camera, material, current, generation=0, maps={}, debugMaterial, loadedMapKey='', captureChain=Promise.resolve(), previewShape='plane', activeView='full';
let awakeUntil=0,paused=false,graphicsLost=false;
function wake(){if(!engine||paused||graphicsLost)return;awakeUntil=performance.now()+1200;engine.runRenderLoop(renderFrame);}
const renderFrame=()=>{const start=performance.now();scene?.render();
  prismPerformance.count('frames');prismPerformance.span('frame',start);
  if(start>awakeUntil&&scene?.isReady())engine.stopRenderLoop(renderFrame);
};
let debugMaps={};
let modelContainer,shapeGeneration=0,shapeWork=null;
function isCurrent(data,ticket){return ticket===generation&&current===data;}
function requestId(data){return data.requestId||data.token;}
function clearShapeWork(){shapeWork?.supersede();shapeWork=null;}
function prepareShape(data,ticket,mesh){
  clearShapeWork();
  const work={data,ticket,mesh,shapeTicket:shapeGeneration,ready:false,error:null,reported:false};
  work.changed=new Promise(resolve=>{work.supersede=resolve;});
  work.valid=()=>isCurrent(data,ticket)&&shapeWork===work&&work.shapeTicket===shapeGeneration&&sphere===mesh;
  shapeWork=work;
  work.promise=(async()=>{
    if(data.maps.displacement)await displacement(data.maps.displacement,mesh,data.properties.controls?.displacement,work.valid);
  })().catch(error=>{if(work.valid())work.error=error;}).then(()=>{work.ready=true;});
  return work;
}
function reportShapeError(work){
  if(!work.valid()||!work.error||work.reported||paused)return;
  work.reported=true;status.textContent=work.error.message;
  bridge.error(requestId(work.data),work.error.message);
}
async function latestShape(data,ticket){
  while(isCurrent(data,ticket)){
    const work=shapeWork;
    if(!work)return {mesh:sphere,shapeTicket:shapeGeneration,work:null};
    if(work.data!==data||work.ticket!==ticket)return null;
    // A replaced Image may never settle; replacement/release must wake the waiter.
    await Promise.race([work.promise,work.changed]);
    if(!isCurrent(data,ticket))return null;
    if(shapeWork!==work)continue;
    if(work.error){reportShapeError(work);return null;}
    if(work.valid())return {mesh:work.mesh,shapeTicket:work.shapeTicket,work};
  }
  return null;
}
function sameShape(shape){return sphere===shape.mesh&&shapeGeneration===shape.shapeTicket&&shapeWork===shape.work;}
function frameSubmitted(data,ticket){
  if(!isCurrent(data,ticket)||paused)return false;
  scene.render();
  // CPU-side scene submission, not proof of compositor/display presentation.
  bridge.frame_submitted(requestId(data),data.signature||'');
  return true;
}
function disposeTextures(textures){for(const t of new Set(textures))t?.dispose();}
function releaseMaterial(keepModel=false){
  if(!keepModel)window.prismModel?.release();
  ++generation;current=null;loadedMapKey='';clearShapeWork();
  if(modelContainer){modelContainer.dispose();modelContainer=null;sphere=null;}
  if(sphere){sphere.dispose();sphere=null;}
  if(material){material.dispose(false,false);material=null;}
  if(debugMaterial){debugMaterial.dispose(false,false);debugMaterial=null;}
  disposeTextures([...Object.values(maps),...Object.values(debugMaps)]);
  maps={};debugMaps={};
  engine?.stopRenderLoop(renderFrame);
}
const canvas=document.getElementById('render'),status=document.getElementById('status');
const B=BABYLON;
let sharedProfile;
function previewProfile(profile){
  // Old hosts can still have requests in flight when the bundled page changes.
  // Read the same shipped configuration as Python instead of duplicating defaults.
  if(profile)return Promise.resolve(profile);
  if(!sharedProfile)sharedProfile=new Promise((resolve,reject)=>{
    const request=new XMLHttpRequest();
    request.open('GET','preview-profile.json');
    request.onload=()=>{
      try{
        if(request.status!==0&&request.status!==200)throw new Error('HTTP '+request.status);
        resolve(JSON.parse(request.responseText));
      }catch(error){reject(new Error('无法读取公共预览配置：'+error.message));}
    };
    request.onerror=()=>reject(new Error('无法读取公共预览配置'));
    request.send();
  }).catch(error=>{sharedProfile=null;throw error;});
  return sharedProfile;
}
function front(){if(camera){camera.mode=B.Camera.PERSPECTIVE_CAMERA;camera.upVector=B.Vector3.Up();camera.fov=.8;
  camera.minZ=.05;camera.maxZ=1000;camera.lowerRadiusLimit=2;camera.upperRadiusLimit=10;
  camera.alpha=-Math.PI/2;camera.beta=Math.PI/2;camera.radius=3.6;camera.target=B.Vector3.Zero();wake();}}
function texture(url,srgb=false,invertY=true){return new Promise((resolve,reject)=>{
  const t=new B.Texture(url,scene,false,invertY,B.Texture.TRILINEAR_SAMPLINGMODE,
    ()=>Promise.resolve().then(()=>{t.gammaSpace=srgb;resolve(t);}),()=>Promise.resolve().then(()=>{t.dispose();reject(new Error('无法加载预览贴图'));}));
});}
async function init(profile){
  if(graphicsLost)throw new Error('图形设备已断开，请关闭并重新打开预览');
  if(engine)return;
  try { engine=new B.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:true,doNotHandleContextLost:true}); }
  catch(error){engine=undefined;throw new Error('WebGL 初始化失败：'+error.message);}
  bridge.metrics(JSON.stringify({gpu:engine.getGlInfo(),webgl:engine.webGLVersion,
    device_pixel_ratio:devicePixelRatio,canvas_width:engine.getRenderWidth(),canvas_height:engine.getRenderHeight()}));
  canvas.addEventListener('webglcontextlost',()=>{
    graphicsLost=true;engine.stopRenderLoop();
    const message='图形设备已断开，已停止渲染，请关闭并重新打开预览';
    status.textContent=message;
    bridge.model_event?.('material.graphics_lost',JSON.stringify({reason:'webgl_context_lost'}));
    if(current)bridge.error(requestId(current),message);
  },{once:true});
  scene=new B.Scene(engine);scene.clearColor=new B.Color4(0,0,0,1);
  camera=new B.ArcRotateCamera('camera',-Math.PI/2,Math.PI/2,3.6,B.Vector3.Zero(),scene);
  camera.minZ=.05;camera.lowerRadiusLimit=2;camera.upperRadiusLimit=10;
  camera.attachControl(canvas,true);front();
  for(const light of profile.lights){
    const source=new B.DirectionalLight(light.name,B.Vector3.FromArray(light.direction),scene);
    source.intensity=light.intensity;
  }
  scene.imageProcessingConfiguration.toneMappingEnabled=true;
  scene.imageProcessingConfiguration.toneMappingType=B.ImageProcessingConfiguration.TONEMAPPING_ACES;
  scene.imageProcessingConfiguration.exposure=profile.exposure;
  const environment=B.CubeTexture.CreateFromPrefilteredData(profile.environment,scene);
  scene.environmentTexture=environment;scene.environmentIntensity=profile.environmentIntensity;
  wake();window.addEventListener('resize',()=>{engine.resize();wake();});
  for(const event of ['pointerdown','pointermove','pointerup','wheel','keydown']){
    canvas.addEventListener(event,wake,{passive:true});
  }
  environment.onLoadObservable.addOnce(wake);
}
async function displacementPixels(url,valid=()=>true){
  const image=new Image();
  await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('置换贴图加载失败'));image.src=url;});
  if(!valid())return null;
  const c=document.createElement('canvas');c.width=image.width;c.height=image.height;
  const ctx=c.getContext('2d');ctx.drawImage(image,0,0);
  return {data:ctx.getImageData(0,0,c.width,c.height).data,width:c.width,height:c.height};
}
async function displacement(url,mesh,options={},valid=()=>true,pixels=null){
  const started=performance.now();
  pixels=pixels||await displacementPixels(url,valid);
  if(!valid()||!pixels)return;
  const strength=options.value??.07,midpoint=options.midpoint??.5;
  mesh.applyDisplacementMapFromBuffer(pixels.data,pixels.width,pixels.height,-midpoint*strength,(1-midpoint)*strength);
  prismPerformance.span('displacement',started);
}
function applySettings(data,target=material,textures=maps){
  return applyMaterialSettings(data,target,textures);
}
function applyMaterialSettings(data,material,maps){
  const c=data.properties.controls||{},get=k=>c[k]||{};
  const amount=(key,fallback)=>get(key).enabled===false?fallback:(get(key).amount??fallback);
  const color=key=>{
    const control=get(key);
    if(control.linearColor)return B.Color3.FromArray(control.linearColor);
    const rgb=control.color||data.previewProfile.untexturedColor;
    const linear=key==='basecolor'&&data.properties.basecolor_color_space==='linear';
    return B.Color3.FromArray(linear?rgb:rgb.map(v=>Math.pow(v,data.previewProfile.colorGamma)));
  };
  material.albedoTexture=maps.basecolor||null;
  material.albedoColor=color('basecolor').scale(amount('basecolor',1));
  if(!maps.basecolor&&!c.basecolor)material.albedoColor=B.Color3.FromArray(data.previewProfile.untexturedColor);
  if(maps.basecolor)maps.basecolor.hasAlpha=false;
  material.metallic=amount('metallic',0);material.roughness=amount('roughness',.5);
  material.metallicTexture=maps.metallicRoughness||null;
  material.useRoughnessFromMetallicTextureAlpha=false;
  material.useRoughnessFromMetallicTextureGreen=true;
  material.useMetallnessFromMetallicTextureBlue=true;
  material.bumpTexture=maps.normal||null;
  if(maps.normal)maps.normal.level=amount('normal',1);
  material.invertNormalMapX=data.geometryUVOrigin==='top-left';
  material.invertNormalMapY=(data.properties.normalConvention||data.previewProfile.normalConvention)==='DirectX';
  material.twoSidedLighting=data.properties.double_sided===true;
  material.ambientTexture=maps.ao||null;material.useAmbientInGrayScale=true;
  material.ambientTextureStrength=amount('ao',1);
  material.emissiveTexture=maps.emissive||null;
  material.emissiveColor=get('emissive').enabled===false?B.Color3.Black():color('emissive').scale(amount('emissive',maps.emissive?1:0));
  const mode=data.properties.blendMode||'opaque';
  material.transparencyMode=mode==='masked'?B.PBRMaterial.PBRMATERIAL_ALPHATEST:mode==='translucent'?B.PBRMaterial.PBRMATERIAL_ALPHABLEND:B.PBRMaterial.PBRMATERIAL_OPAQUE;
  material.alpha=amount('opacity',1);material.alphaCutOff=data.properties.alphaCutoff??.5;
  material.useAlphaFromAlbedoTexture=false;
  material.opacityTexture=mode!=='opaque'?(maps.opacity||null):null;
  if(material.opacityTexture){
    material.opacityTexture.getAlphaFromRGB=true;
    // Babylon's PBR OPACITYRGB path replaces base alpha with luminance.
    // Its native texture level then applies the user's opacity multiplier.
    material.opacityTexture.level=material.alpha;
  }
  // Babylon's native thin-surface Burley transport, with the chosen SSS mask.
  material.subSurface.isTranslucencyEnabled=!!maps.sss;
  if(maps.sss){
    material.subSurface.useGltfStyleTextures=false;
    material.subSurface.translucencyIntensityTexture=maps.sss;
      material.subSurface.translucencyColorTexture=data.sssColor?maps.sss:null;
    material.subSurface.translucencyIntensity=amount('sss',1);
    material.subSurface.translucencyColor=color('sss');
    material.subSurface.useAlbedoToTintTranslucency=true;
    material.subSurface.minimumThickness=.1;material.subSurface.maximumThickness=.1;
    material.subSurface.diffusionDistance=new B.Color3(1,.5,.3);
  }
}
async function finish(data,ticket,compile=true){
  while(isCurrent(data,ticket)){
    const shape=await latestShape(data,ticket);
    if(!shape||!isCurrent(data,ticket))return;
    if(!sameShape(shape))continue;
    if(compile){
      const started=performance.now();
      wake(); // Texture loading may have outlived the previous render loop.
      await new Promise((resolve,reject)=>{
        const timeout=setTimeout(()=>reject(new Error('材质编译超时')),30000);
        scene.executeWhenReady(()=>{clearTimeout(timeout);resolve();});
      });
      if(!isCurrent(data,ticket))return;
      prismPerformance.span('wait_scene_ready',started);
      if(!sameShape(shape))continue;
      await new Promise(resolve=>{
        let done=false;
        const finish=()=>{if(done)return;done=true;clearTimeout(timer);resolve();};
        const timer=setTimeout(finish,250);
        requestAnimationFrame(()=>requestAnimationFrame(finish));
      });
      if(!isCurrent(data,ticket))return;
      if(!sameShape(shape))continue;
    }
    status.textContent='';applyView(activeView);
    if(!frameSubmitted(data,ticket)||!isCurrent(data,ticket))return;
    if(!sameShape(shape))continue;
    bridge.updated(requestId(data),data.signature||'');
    break;
  }
  if(!isCurrent(data,ticket))return;
  wake();
  if(data.capture!==false)await capture(data.token,requestId(data),data.signature||'');
  if(!isCurrent(data,ticket))return;
  window.prismRender.diagnostics={maps:Object.keys(maps),mode:data.properties.blendMode,
    sss:!!maps.sss,displacement:!!maps.displacement,ready:material.isReady(sphere),
    roughness:material.roughness,metallic:material.metallic,alpha:material.alpha,
    albedo:material.albedoColor.asArray(),webGL:engine.webGLVersion,
    vertices:sphere.getTotalVertices(),shape:previewShape,view:activeView};
}
function applyView(slot){
  activeView=slot;
  if(!sphere||!material)return;
  if(slot==='full'){sphere.material=material;status.textContent='';return;}
  if(!maps[slot]){sphere.material=material;status.textContent='这个槽位未配置贴图';return;}
  if(!debugMaterial){
    debugMaterial=new B.PBRMaterial('channel',scene);
    debugMaterial.unlit=true;
    debugMaterial.albedoColor=B.Color3.White();
    debugMaterial.emissiveColor=B.Color3.Black();
    debugMaterial.metallic=0;
    debugMaterial.roughness=1;
    debugMaterial.backFaceCulling=false;
    debugMaterial.imageProcessingConfiguration=new B.ImageProcessingConfiguration();
    debugMaterial.imageProcessingConfiguration.toneMappingEnabled=false;
  }
  if(!debugMaps[slot]){debugMaps[slot]=maps[slot].clone();debugMaps[slot].gammaSpace=true;}
  debugMaterial.albedoTexture=debugMaps[slot];
  sphere.material=debugMaterial;status.textContent='';
}
async function materialBallImage(captureMaterial,size=256){
  const thumbnail=B.MeshBuilder.CreateSphere('thumbnail',{diameter:2,segments:48},scene);
  thumbnail.material=captureMaterial;thumbnail.layerMask=0x10000000;
  const thumbnailCamera=new B.ArcRotateCamera('thumbnail-camera',-Math.PI/2,Math.PI/2,3.6,B.Vector3.Zero(),scene);
  thumbnailCamera.layerMask=0x10000000;
  const draw=()=>scene.render();engine.runRenderLoop(draw);
  try{return await B.Tools.CreateScreenshotUsingRenderTargetAsync(engine,thumbnailCamera,{width:size,height:size});}
  finally{engine.stopRenderLoop(draw);thumbnail.dispose();thumbnailCamera.dispose();wake();}
}
async function capture(token,identity,signature){
  const data=current,ticket=generation,captureMaterial=material;
  if(!data||data.token!==token||!captureMaterial||paused
      ||requestId(data)!==identity||(data.signature||'')!==signature)return;
  const work=captureChain.then(async()=>{
    if(!isCurrent(data,ticket)||paused||material!==captureMaterial)return;
    const started=performance.now();
    try{
      const image=await materialBallImage(captureMaterial);
      if(isCurrent(data,ticket)&&!paused)bridge.rendered(identity,signature,image);
    }finally{prismPerformance.span('thumbnail_capture',started);if(isCurrent(data,ticket)&&!paused)applyView(activeView);}
  });
  captureChain=work.catch(()=>{});
  try{await work;}catch(error){
    if(isCurrent(data,ticket)&&!paused){status.textContent=error.message;bridge.error(identity,error.message);}
  }
}
function makeMesh(){
  const displaced=!!current?.maps?.displacement;
  if(previewShape==='sphere')return B.MeshBuilder.CreateSphere('material-sphere',{diameter:2,segments:displaced?128:48},scene);
  if(previewShape==='cylinder')return B.MeshBuilder.CreateCylinder('material-cylinder',{diameter:1.8,height:2,tessellation:64,subdivisions:displaced?32:1},scene);
  if(previewShape==='box')return B.MeshBuilder.CreateBox('material-box',{size:1.65},scene);
  return B.MeshBuilder.CreateGround('material-plane',{width:2,height:2,subdivisions:displaced?128:1},scene);
}
async function setShape(shape){
  if(!['sphere','cylinder','box','plane'].includes(shape))return;
  previewShape=shape;++shapeGeneration;clearShapeWork();
  const data=current,ticket=generation;
  if(!material||!data||data.model)return;
  if(sphere)sphere.dispose();sphere=makeMesh();
  const mesh=sphere;
  if(shape==='plane'){mesh.rotation.x=-Math.PI/2;material.backFaceCulling=false;}
  mesh.material=material;
  const work=prepareShape(data,ticket,mesh),valid=work.valid;
  try{
    await work.promise;
    if(!valid()||paused)return;
    if(work.error){reportShapeError(work);return;}
    front();
    if(shape==='box'){camera.alpha=-Math.PI/4;camera.beta=Math.PI/3;}
    if(shape==='cylinder')camera.beta=Math.PI/2.6;
    applyView(activeView);scene.render();wake();
  }catch(error){if(valid()&&!paused)bridge.error(requestId(data),error.message);}
}
window.prismRender={
  front, capture, shape:setShape, release:releaseMaterial,
  pause(value){paused=value;if(!engine)return;value?engine.stopRenderLoop(renderFrame):wake();},
  async load(data){
    if(window.prismModel?.active)window.prismModel.release();
    const started=performance.now();prismPerformance.count('load_requests');
    const shapeAtStart=shapeGeneration;
    const previousShapeReady=!shapeWork||(shapeWork.ready&&!shapeWork.error&&shapeWork.mesh===sphere);
    const ticket=++generation,previous=current;clearShapeWork();current=data;status.textContent='正在更新…';
    try{
      data.previewProfile=await previewProfile(data.previewProfile);
      if(!isCurrent(data,ticket))return;
      await init(data.previewProfile);
      if(!isCurrent(data,ticket))return;
      const mapKey=JSON.stringify(data.maps||{});
      const reuse=material&&mapKey===loadedMapKey;
      const sameDisplacement=JSON.stringify(previous?.properties?.controls?.displacement)===JSON.stringify(data.properties?.controls?.displacement);
      activeView=data.view||activeView;
      // A user choice made during init is newer than this payload's snapshot.
      const requestedShape=shapeGeneration===shapeAtStart?(data.shape||previewShape):previewShape;
      const shapeChanged=requestedShape!==previewShape;previewShape=requestedShape;
      if(!data.model&&!previous?.model&&reuse&&sameDisplacement&&!shapeChanged&&previousShapeReady){
        sphere.material=material;applySettings(data);await finish(data,ticket,false);
        if(!isCurrent(data,ticket))return;
        return;
      }
      if(data.model){
        const container=await B.SceneLoader.LoadAssetContainerAsync('',data.model,scene);
        if(!isCurrent(data,ticket)){container.dispose();return;}
        if(modelContainer)modelContainer.dispose();
        modelContainer=container;
        if(sphere)sphere.dispose();
        container.addAllToScene();
        const root=container.createRootMesh();root.normalizeToUnitCube(true);root.scaling.scaleInPlace(2);
        root.computeWorldMatrix(true);
        const bounds=root.getHierarchyBoundingVectors(true);
        root.position.subtractInPlace(bounds.min.add(bounds.max).scale(.5));
        sphere=root;
        front();engine.resize();
        await scene.whenReadyAsync();
        if(!isCurrent(data,ticket)||modelContainer!==container)return;
        if(!frameSubmitted(data,ticket)||!isCurrent(data,ticket))return;
        status.textContent='';
        window.prismRender.diagnostics={loader:'Babylon.js glTF 2.0',meshes:container.meshes.length,materials:container.materials.length,textures:container.textures.length};
        bridge.loaded(requestId(data),data.signature||'');
        return;
      }
      const texturesStarted=performance.now();
      let entries;
      if(reuse)entries=Object.entries(maps);
      else {
        const results=await Promise.allSettled(Object.entries(data.maps).map(async([key,url])=>[key,await texture(url,['basecolor','emissive'].includes(key))]));
        entries=results.filter(result=>result.status==='fulfilled').map(result=>result.value);
        const failure=results.find(result=>result.status==='rejected');
        if(failure){disposeTextures(entries.map(([,t])=>t));throw failure.reason;}
      }
      prismPerformance.span(reuse?'reuse_textures':'texture_load',texturesStarted);
      if(!isCurrent(data,ticket)){if(!reuse)disposeTextures(entries.map(([,t])=>t));return;}
      if(modelContainer){modelContainer.dispose();modelContainer=null;}
      if(sphere)sphere.dispose();if(material)material.dispose(false,false);if(debugMaterial){debugMaterial.dispose(false,false);debugMaterial=null;}
      if(!reuse){disposeTextures([...Object.values(debugMaps),...Object.values(maps)]);debugMaps={};}maps=Object.fromEntries(entries);loadedMapKey=mapKey;
      sphere=makeMesh();if(previewShape==='plane')sphere.rotation.x=-Math.PI/2;
      material=new B.PBRMaterial('material',scene);material.backFaceCulling=false;sphere.material=material;
      applySettings(data);
      prepareShape(data,ticket,sphere);
      if(!previous||previous.recordId!==data.recordId)front();engine.resize();
      await finish(data,ticket);
      if(!isCurrent(data,ticket))return;
    }catch(error){if(ticket===generation&&(!current||current===data)&&!paused){status.textContent=error.message;bridge.error(requestId(data),error.message);}}
    finally{prismPerformance.span('load',started);}
  },
  view(slot){applyView(slot);scene?.render();wake();}

};
new QWebChannel(qt.webChannelTransport,c=>{bridge=c.objects.materialRender;
  prismPerformance.connect(value=>bridge.metrics(value));
  const ready=()=>bridge.ready();
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ready,{once:true});else ready();});
