/* Babylon.js supplies PBR lighting, subsurface transport and mesh displacement. */
'use strict';
let bridge, engine, scene, sphere, camera, material, current, generation=0, maps={}, debugMaterial, loadedMapKey='', captureChain=Promise.resolve(), previewShape='plane', activeView='full';
let awakeUntil=0,paused=false;
function wake(){if(!engine||paused)return;awakeUntil=performance.now()+1200;engine.runRenderLoop(renderFrame);}
const renderFrame=()=>{const start=performance.now();scene?.render();
  prismPerformance.count('frames');prismPerformance.span('frame',start);
  if(start>awakeUntil&&scene?.isReady())engine.stopRenderLoop(renderFrame);
};
let debugMaps={};
const canvas=document.getElementById('render'),status=document.getElementById('status');
const B=BABYLON;
function front(){if(camera){camera.alpha=-Math.PI/2;camera.beta=Math.PI/2;camera.radius=3.6;camera.target=B.Vector3.Zero();wake();}}
function texture(url,srgb=false){return new Promise((resolve,reject)=>{
  const t=new B.Texture(url,scene,false,true,B.Texture.TRILINEAR_SAMPLINGMODE,
    ()=>{t.gammaSpace=srgb;resolve(t);},()=>reject(new Error('无法加载预览贴图')));
});}
async function init(){
  if(engine)return;
  try { engine=new B.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:true}); }
  catch(error){engine=undefined;throw new Error('WebGL 初始化失败：'+error.message);}
  bridge.metrics(JSON.stringify({gpu:engine.getGlInfo(),webgl:engine.webGLVersion,
    device_pixel_ratio:devicePixelRatio,canvas_width:engine.getRenderWidth(),canvas_height:engine.getRenderHeight()}));
  scene=new B.Scene(engine);scene.clearColor=new B.Color4(0,0,0,1);
  camera=new B.ArcRotateCamera('camera',-Math.PI/2,Math.PI/2,3.6,B.Vector3.Zero(),scene);
  camera.minZ=.05;camera.lowerRadiusLimit=2;camera.upperRadiusLimit=10;
  camera.attachControl(canvas,true);front();
  const key=new B.DirectionalLight('key',new B.Vector3(-1,-1,1),scene);key.intensity=2.2;
  const rim=new B.DirectionalLight('rim',new B.Vector3(1,.3,-1),scene);rim.intensity=1.4;
  scene.imageProcessingConfiguration.toneMappingEnabled=true;
  scene.imageProcessingConfiguration.toneMappingType=B.ImageProcessingConfiguration.TONEMAPPING_ACES;
  const environment=B.CubeTexture.CreateFromPrefilteredData('babylon/studio.env',scene);
  scene.environmentTexture=environment;scene.environmentIntensity=.8;
  wake();window.addEventListener('resize',()=>{engine.resize();wake();});
  for(const event of ['pointerdown','pointermove','pointerup','wheel','keydown']){
    canvas.addEventListener(event,wake,{passive:true});
  }
  environment.onLoadObservable.addOnce(wake);
}
async function displacement(url,mesh,options={}){
  const started=performance.now();
  const image=new Image();image.src=url;
  await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('置换贴图加载失败'));});
  const c=document.createElement('canvas');c.width=image.width;c.height=image.height;
  const ctx=c.getContext('2d');ctx.drawImage(image,0,0);
  const strength=options.value??.07,midpoint=options.midpoint??.5;
  mesh.applyDisplacementMapFromBuffer(ctx.getImageData(0,0,c.width,c.height).data,c.width,c.height,-midpoint*strength,(1-midpoint)*strength);
  prismPerformance.span('displacement',started);
}
function applySettings(data){
  const c=data.properties.controls||{},get=k=>c[k]||{};
  const amount=(key,fallback)=>get(key).enabled===false?fallback:(get(key).amount??fallback);
  const color=key=>B.Color3.FromArray(get(key).color||[1,1,1]).toLinearSpace();
  material.albedoTexture=maps.basecolor||null;
  material.albedoColor=color('basecolor').scale(amount('basecolor',1));
  if(!maps.basecolor&&!c.basecolor)material.albedoColor=new B.Color3(.65,.65,.65);
  if(maps.basecolor)maps.basecolor.hasAlpha=false;
  material.metallic=amount('metallic',0);material.roughness=amount('roughness',.5);
  material.metallicTexture=maps.metallicRoughness||null;
  material.useRoughnessFromMetallicTextureAlpha=false;
  material.useRoughnessFromMetallicTextureGreen=true;
  material.useMetallnessFromMetallicTextureBlue=true;
  material.bumpTexture=maps.normal||null;
  if(maps.normal)maps.normal.level=amount('normal',1);
  material.invertNormalMapX=false;
  material.invertNormalMapY=(data.properties.normalConvention||'DirectX')==='DirectX';
  material.ambientTexture=maps.ao||null;material.useAmbientInGrayScale=true;
  material.ambientTextureStrength=amount('ao',1);
  material.emissiveTexture=maps.emissive||null;
  material.emissiveColor=get('emissive').enabled===false?B.Color3.Black():color('emissive').scale(amount('emissive',maps.emissive?1:0));
  const mode=data.properties.blendMode||'opaque';
  material.transparencyMode=mode==='masked'?B.PBRMaterial.PBRMATERIAL_ALPHATEST:mode==='translucent'?B.PBRMaterial.PBRMATERIAL_ALPHABLEND:B.PBRMaterial.PBRMATERIAL_OPAQUE;
  material.alpha=amount('opacity',1);material.alphaCutOff=data.properties.alphaCutoff??.5;
  material.useAlphaFromAlbedoTexture=false;
  material.opacityTexture=mode!=='opaque'?(maps.opacity||null):null;
  if(material.opacityTexture)material.opacityTexture.getAlphaFromRGB=true;
  // Babylon's native thin-surface Burley transport, with the chosen SSS mask.
  material.subSurface.isTranslucencyEnabled=!!maps.sss;
  if(maps.sss){
    material.subSurface.useGltfStyleTextures=false;
    material.subSurface.translucencyIntensityTexture=maps.sss;
    material.subSurface.translucencyIntensity=amount('sss',1);
    material.subSurface.translucencyColor=color('sss');
    material.subSurface.useAlbedoToTintTranslucency=true;
    material.subSurface.minimumThickness=.1;material.subSurface.maximumThickness=.1;
    material.subSurface.diffusionDistance=new B.Color3(1,.5,.3);
  }
}
async function finish(data,ticket,compile=true){
  if(compile){
  const started=performance.now();
  await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(new Error('材质编译超时')),30000);
    scene.executeWhenReady(()=>{clearTimeout(timeout);resolve();});
  });
  prismPerformance.span('wait_scene_ready',started);
  if(ticket!==generation)return;
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  }
  if(ticket!==generation)return;
  scene.render();status.textContent='';
  applyView(activeView);
  bridge.updated(data.token,data.signature);
  wake();
  if(data.capture!==false)await capture(data.token);
  if(ticket!==generation)return;
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
async function capture(token){
  const data=current,ticket=generation;
  if(!data||data.token!==token||!material)return;
  const work=captureChain.then(async()=>{
    if(ticket!==generation)return;
    const started=performance.now();
    const thumbnail=B.MeshBuilder.CreateSphere('thumbnail',{diameter:2,segments:48},scene);
    thumbnail.material=material;thumbnail.layerMask=0x10000000;
    const thumbnailCamera=new B.ArcRotateCamera('thumbnail-camera',-Math.PI/2,Math.PI/2,3.6,B.Vector3.Zero(),scene);
    thumbnailCamera.layerMask=0x10000000;
    try{
      const image=await B.Tools.CreateScreenshotUsingRenderTargetAsync(engine,thumbnailCamera,{width:256,height:256});
      if(ticket===generation)bridge.rendered(data.token,data.signature,image);
    }finally{prismPerformance.span('thumbnail_capture',started);thumbnail.dispose();thumbnailCamera.dispose();if(ticket===generation)applyView(activeView);}
  });
  captureChain=work.catch(()=>{});await work;
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
  previewShape=shape;
  if(!material)return;
  if(sphere)sphere.dispose();sphere=makeMesh();
  if(shape==='plane'){sphere.rotation.x=-Math.PI/2;material.backFaceCulling=false;}
  sphere.material=material;
  if(current.maps.displacement)await displacement(current.maps.displacement,sphere,current.properties.controls?.displacement);
  front();
  if(shape==='box'){camera.alpha=-Math.PI/4;camera.beta=Math.PI/3;}
  if(shape==='cylinder')camera.beta=Math.PI/2.6;
  applyView(activeView);scene.render();wake();
}
window.prismRender={
  front, capture, shape:setShape,
  pause(value){paused=value;if(!engine)return;value?engine.stopRenderLoop(renderFrame):wake();},
  async load(data){
    const started=performance.now();prismPerformance.count('load_requests');
    const ticket=++generation,previous=current;status.textContent='正在更新…';
    try{
      await init();
      const mapKey=JSON.stringify(data.maps||{});
      const reuse=material&&mapKey===loadedMapKey;
      const sameDisplacement=JSON.stringify(previous?.properties?.controls?.displacement)===JSON.stringify(data.properties?.controls?.displacement);
      current=data;activeView=data.view||activeView;
      const shapeChanged=data.shape&&data.shape!==previewShape;previewShape=data.shape||previewShape;
      if(!data.model&&reuse&&sameDisplacement&&!shapeChanged){
        sphere.material=material;applySettings(data);await finish(data,ticket,false);return;
      }
      if(data.model){
        const container=await B.SceneLoader.LoadAssetContainerAsync('',data.model,scene);
        if(ticket!==generation){container.dispose();return;}
        if(sphere)sphere.dispose();
        container.addAllToScene();
        const root=container.createRootMesh();root.normalizeToUnitCube(true);root.scaling.scaleInPlace(2);
        root.computeWorldMatrix(true);
        const bounds=root.getHierarchyBoundingVectors(true);
        root.position.subtractInPlace(bounds.min.add(bounds.max).scale(.5));
        sphere=root;
        front();engine.resize();
        await scene.whenReadyAsync();
        status.textContent='';
        window.prismRender.diagnostics={loader:'Babylon.js glTF 2.0',meshes:container.meshes.length,materials:container.materials.length,textures:container.textures.length};
        bridge.loaded(data.token);
        return;
      }
      const texturesStarted=performance.now();
      const entries=reuse?Object.entries(maps):await Promise.all(Object.entries(data.maps).map(async([key,url])=>[key,await texture(url,['basecolor','emissive'].includes(key))]));
      prismPerformance.span(reuse?'reuse_textures':'texture_load',texturesStarted);
      if(ticket!==generation){if(!reuse)entries.forEach(([,t])=>t.dispose());return;}
      if(sphere)sphere.dispose();if(material)material.dispose(false,false);if(debugMaterial){debugMaterial.dispose(false,false);debugMaterial=null;}
      if(!reuse){Object.values(debugMaps).forEach(t=>t.dispose());debugMaps={};Object.values(maps).forEach(t=>t.dispose());}maps=Object.fromEntries(entries);loadedMapKey=mapKey;
      sphere=makeMesh();if(previewShape==='plane')sphere.rotation.x=-Math.PI/2;
      material=new B.PBRMaterial('material',scene);material.backFaceCulling=false;sphere.material=material;
      applySettings(data);
      if(data.maps.displacement)await displacement(data.maps.displacement,sphere,data.properties.controls?.displacement);
      if(!previous||previous.recordId!==data.recordId)front();engine.resize();
      await finish(data,ticket);
    }catch(error){if(ticket===generation){status.textContent=error.message;bridge.error(data.token,error.message);}}
    finally{prismPerformance.span('load',started);}
  },
  view(slot){applyView(slot);scene?.render();wake();}

};
new QWebChannel(qt.webChannelTransport,c=>{bridge=c.objects.materialRender;
  prismPerformance.connect(value=>bridge.metrics(value));bridge.ready();});
