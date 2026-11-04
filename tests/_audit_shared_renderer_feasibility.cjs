// Standalone headless proof, using the shipped Babylon and production render.js.
// Only the Qt transport is replaced; no renderer, texture, shader or light is mocked.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{pathToFileURL}=require('url');
const {chromium}=require('D:/AITest/node_modules/playwright-core');
const root=path.resolve(__dirname,'..'),out=path.join(root,'artifacts/shared-renderer-feasibility-20261102');
const inputs=JSON.parse(fs.readFileSync(path.join(out,'inputs.json'),'utf8'));
const url=p=>pathToFileURL(path.join(root,p)).href;
const html=`<!doctype html><html><style>html,body{margin:0;width:512px;height:512px}canvas{width:512px;height:512px}</style>
<canvas id="render"></canvas><div id="status"></div><script>
globalThis.qt={webChannelTransport:{}};globalThis.auditErrors=[];
const auditBridge={
ready(){globalThis.auditReady=true},metrics(){},updated(){},frame_submitted(){},rendered(){},loaded(){},
error(id,error){auditErrors.push({id,error})}};
globalThis.QWebChannel=function(_,callback){callback({objects:{materialRender:auditBridge}})};</script>
<script src="${url('prism/assets/materials/babylon/babylon.js')}"></script>
<script src="${url('prism/assets/materials/babylon/babylonjs.loaders.min.js')}"></script>
<script src="${url('prism/assets/materials/performance.js')}"></script>
<script src="${url('prism/assets/materials/render.js')}"></script></html>`;
fs.writeFileSync(path.join(out,'probe.html'),html);
const report={runtime:'Chromium headless / SwiftShader',transport:'Qt bridge stub only',cases:[],consoleErrors:[],
 productionCodeChanged:false,qtOffscreenAttempt:'Native access violation before rendering; not a passing result'};
(async()=>{
 let browser;
 try{
 browser=await chromium.launch({headless:true,
 executablePath:'C:/Users/admin/AppData/Local/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe',
 args:['--allow-file-access-from-files','--use-angle=swiftshader','--enable-unsafe-swiftshader'],timeout:30000});
 const page=await browser.newPage({viewport:{width:512,height:512}});
 page.on('pageerror',e=>report.consoleErrors.push(String(e)));
 await page.goto(pathToFileURL(path.join(out,'probe.html')).href);
 await page.waitForFunction(()=>globalThis.auditReady,null,{timeout:30000});
 for(const input of inputs){
  const started=Date.now();
  await page.evaluate(async data=>{await prismRender.load(data);},input.payload);
  const result=await page.evaluate(async input=>{
   if(auditErrors.length)throw new Error(JSON.stringify(auditErrors));
   engine.stopRenderLoop();
   await scene.whenReadyAsync();scene.render();
   const capture=()=>engine.readPixels(0,0,engine.getRenderWidth(),engine.getRenderHeight());
   const nonblack=p=>{let n=0;for(let i=0;i<p.length;i+=4)if(p[i]+p[i+1]+p[i+2]>15)n++;return n};
   const cloneShared=(source,name)=>{
    const copy=source.clone(name);
    for(const key of ['albedoTexture','bumpTexture','metallicTexture','ambientTexture','emissiveTexture','opacityTexture']){
     if(copy[key]&&copy[key]!==source[key])copy[key].dispose();
     copy[key]=source[key];
    }
    return copy;
   };
   const ball=await capture(),environment=scene.environmentTexture.uniqueId;
   // Identical geometry through the model mesh API must have identical pixels.
   const equivalent=new B.Mesh('audit-equivalent-model',scene);
   B.VertexData.ExtractFromMesh(sphere).applyToMesh(equivalent);
   equivalent.material=cloneShared(material,'audit-equivalent-material');
   equivalent.position.copyFrom(sphere.position);equivalent.rotation.copyFrom(sphere.rotation);
   equivalent.scaling.copyFrom(sphere.scaling);sphere.setEnabled(false);
   await scene.whenReadyAsync();scene.render();const same=await capture();
   let pixelDifference=0;for(let i=0;i<ball.length;i++)if(ball[i]!==same[i])pixelDifference++;
   equivalent.material.dispose(false,false);equivalent.dispose();
   const root=new B.Mesh('audit-root',scene),meshes=[];
   const binary=await new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('GET',input.geometryUrl);xhr.responseType='arraybuffer';
    xhr.onload=()=>resolve(xhr.response);xhr.onerror=()=>reject(new Error('Binary geometry read failed'));xhr.send();
   });
   const floats=part=>new Float32Array(binary,part.offset,part.count);
   const assignments=new Map(input.slots.map(slot=>[slot.binding,slot.material]));
   const slotMaterials=new Map();
   for(const part of input.geometry){
    const mesh=new B.Mesh(part.binding,scene),data=new B.VertexData();
    data.positions=floats(part.positions);data.indices=new Uint32Array(binary,part.indices.offset,part.indices.count);
    data.normals=part.normals?floats(part.normals):[];
    if(!part.normals)B.VertexData.ComputeNormals(data.positions,data.indices,data.normals);
    if(part.uv)data.uvs=floats(part.uv);data.applyToMesh(mesh);
    mesh.setPreTransformMatrix(B.Matrix.FromArray(part.matrix));mesh.parent=root;
    const slotId=assignments.get(part.binding);
    if(!slotMaterials.has(slotId))slotMaterials.set(slotId,cloneShared(material,'slot:'+slotId));
    mesh.metadata={slotId};mesh.material=slotMaterials.get(slotId);meshes.push(mesh);
   }
   root.computeWorldMatrix(true);meshes.forEach(m=>m.computeWorldMatrix(true));
   const bounds=root.getHierarchyBoundingVectors(true),size=bounds.max.subtract(bounds.min);
   camera.target=bounds.max.add(bounds.min).scale(.5);
   camera.radius=Math.max(size.x,size.y,size.z)*1.8;
   camera.minZ=Math.max(camera.radius/10000,.00001);camera.maxZ=camera.radius*100;
   await scene.whenReadyAsync();scene.render();const modelPixels=await capture();
   const vertices=meshes.reduce((n,m)=>n+m.getTotalVertices(),0);
   const bindings=meshes.map(m=>m.name);
   const uvMeshes=meshes.filter(m=>m.isVerticesDataPresent(B.VertexBuffer.UVKind)).length;
   const normalMeshes=meshes.filter(m=>m.isVerticesDataPresent(B.VertexBuffer.NormalKind)).length;
   // Replace one existing slot's content; neither geometry nor slot count grows.
   const slot=meshes[0],original=slot.material,replacement=cloneShared(original,'audit-replacement');
   const originalSlotCount=slotMaterials.size;
   replacement.albedoColor=new B.Color3(1,0,0);
   const targets=meshes.filter(m=>m.metadata.slotId===slot.metadata.slotId);
   targets.forEach(m=>m.material=replacement);
   await scene.whenReadyAsync();scene.render();const replacedPixels=await capture();
   let changed=0;for(let i=0;i<modelPixels.length;i++)if(modelPixels[i]!==replacedPixels[i])changed++;
   const slotIsolation=meshes.filter(m=>m.metadata.slotId!==slot.metadata.slotId)
     .every(m=>m.material===slotMaterials.get(m.metadata.slotId))
     &&targets.every(m=>m.material===replacement)&&slotMaterials.size===originalSlotCount;
   targets.forEach(m=>m.material=original);replacement.dispose(false,false);
   const stats={modelNonblack:nonblack(modelPixels),ballNonblack:nonblack(ball),pixelDifference,
    modelVertices:vertices,bindings,uvMeshes,normalMeshes,slotIsolation,replacementChangedChannels:changed,
    sameMaterial:meshes.every(m=>m.material.getClassName()===material.getClassName()
       &&m.material.albedoTexture===material.albedoTexture&&m.material.bumpTexture===material.bumpTexture
       &&m.material.metallicTexture===material.metallicTexture),
    slotCount:slotMaterials.size,replacedParts:targets.length,
    sameEnvironment:scene.environmentTexture.uniqueId===environment,
    lights:scene.lights.map(l=>({name:l.name,intensity:l.intensity,direction:l.direction.asArray()})),
    webGL:engine.webGLVersion,gpu:engine.getGlInfo(),normalConvention:material.invertNormalMapY?'DirectX':'OpenGL',
    maps:Object.keys(maps),bounds:{min:bounds.min.asArray(),max:bounds.max.asArray()}};
   meshes.forEach(m=>m.dispose());slotMaterials.forEach(m=>m.dispose(false,false));
   root.dispose();sphere.setEnabled(true);front();
   return stats;
  },input);
  result.name=input.name;result.ms=Date.now()-started;result.materials=input.materials;
  result.geometryBytes=input.geometryBytes;
  result.originalHashesUnchanged=Object.entries(input.hashes).every(([file,hash])=>
      crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')===hash);
  report.cases.push(result);fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(result));
  if(!result.modelNonblack||!result.ballNonblack||result.pixelDifference!==0||!result.slotIsolation||
     !result.sameMaterial||!result.sameEnvironment||!result.originalHashesUnchanged||result.replacementChangedChannels===0||
     result.uvMeshes!==input.geometry.length||result.normalMeshes!==input.geometry.length)
      throw new Error('Acceptance failed: '+input.name);
 }
 report.status='passed';
 }catch(error){report.status='failed';report.error=String(error);console.error(error);process.exitCode=1;}
 finally{fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));if(browser)await browser.close();}
})();
