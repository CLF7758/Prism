// Exercise production render.html + model-scene.js, never a desktop window.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),assert=require('assert');
const {pathToFileURL}=require('url'),{chromium}=require('D:/AITest/node_modules/playwright-core');
const root=path.resolve(__dirname,'..'),out=path.join(root,'artifacts/shared-renderer-integration-20261102');
const cases=JSON.parse(fs.readFileSync(path.join(out,'inputs.json'),'utf8'));
const report={renderer:'production Babylon render.html/model-scene.js',runtime:'Chromium headless SwiftShader',cases:[],errors:[]};
(async()=>{let browser;try{
 browser=await chromium.launch({headless:true,executablePath:'C:/Users/admin/AppData/Local/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe',args:['--allow-file-access-from-files','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 const page=await browser.newPage({viewport:{width:512,height:512}});page.on('pageerror',e=>report.errors.push(String(e)));
 page.on('console',message=>{if(message.text().startsWith('audit:'))console.log(message.text());});
 await page.addInitScript(()=>{
  window.qt={webChannelTransport:{}};window.failures=[];
  window.QWebChannel=function(_,callback){callback({objects:{materialRender:{
    ready(){window.bridgeReady=true},metrics(){},updated(){},frame_submitted(){},rendered(){},loaded(){},
    model_result(){},model_event(){},error(id,error){failures.push({id,error})}}}})};
 });
 await page.goto(pathToFileURL(path.join(root,'prism/assets/materials/render.html')).href);
 await page.waitForFunction(()=>window.bridgeReady&&window.prismModel);
 for(const input of cases){
  const result=await page.evaluate(async input=>Promise.race([(async()=>{
   const check=(condition,message)=>{if(!condition)throw new Error(message)};
   const stage=value=>{window.auditStage=value;console.log('audit:'+input.name+':'+value)};
   stage('ball');
   const payload={...input.ball,previewProfile:input.scene.profile};
   await prismRender.load(payload);check(!failures.length,JSON.stringify(failures));
   stage('ball-loaded');scene.render();
   const ballShader={albedo:material.albedoColor.asArray(),roughness:material.roughness,metallic:material.metallic,invertY:material.invertNormalMapY};
   const ballImage=await materialBallImage(material,128);
   stage('ball-captured');
   const environment=scene.environmentTexture.uniqueId;
   stage('model-load');
   const first=await prismModel.load(input.scene);engine.stopRenderLoop();
   const model=await engine.readPixels(0,0,engine.getRenderWidth(),engine.getRenderHeight());
   let nonblack=0;for(let i=0;i<model.length;i+=4)if(model[i]+model[i+1]+model[i+2]>15)nonblack++;
   const ids=()=>scene.meshes.filter(m=>m.metadata?.binding).map(m=>m.uniqueId);
   const meshIds=ids(),textures=scene.textures.map(t=>t.uniqueId),cameraBefore=prismModel.cameraState();
   const updated=structuredClone(input.scene),wid=Object.values(updated.assignments)[0];
   stage('material-edit');
   updated.materials[wid].properties.controls.basecolor.color=[1,0,0];
   updated.materials[wid].properties.controls.basecolor.linearColor=[1,0,0];
   const second=await prismModel.load(updated);engine.stopRenderLoop();
   check(JSON.stringify(meshIds)===JSON.stringify(ids()),'Scalar edit rebuilt geometry');
   check(JSON.stringify(cameraBefore)===JSON.stringify(prismModel.cameraState()),'Scalar edit changed camera');
   check(JSON.stringify(textures)===JSON.stringify(scene.textures.map(t=>t.uniqueId)),'Scalar edit copied textures');
   if(input.name==='WoodCarving'){
    const source=scene.meshes.find(m=>m.metadata?.binding),base=Array.from(source.getVerticesData(B.VertexBuffer.PositionKind));
    const displaced=structuredClone(updated);
    displaced.materials[wid].maps.displacement=displaced.materials[wid].maps.roughness;
    displaced.materials[wid].properties.controls.displacement.value=.07;
    await prismModel.load(displaced);
    check(source.getVerticesData(B.VertexBuffer.PositionKind).some((value,index)=>value!==base[index]),'Displacement did not change geometry');
    await prismModel.load(updated);
    check(source.getVerticesData(B.VertexBuffer.PositionKind).every((value,index)=>value===base[index]),'Clearing displacement did not restore geometry');
   }
   check(first.resources.materials===second.resources.materials,'Replacement grew slot count');
   const visible=scene.meshes.filter(m=>m.metadata?.binding&&m.isEnabled());
   const targetIds=Object.keys(updated.assignments).filter(id=>updated.assignments[id]===wid);
   check(visible.filter(m=>targetIds.includes(m.name)).every(m=>m.material.albedoColor.r===1&&m.material.albedoColor.g===0),'Current slot not replaced');
   check(visible.filter(m=>!targetIds.includes(m.name)).every(m=>m.material.albedoColor.g===ballShader.albedo[1]),'Replacement changed another slot');
   const sameEnvironment=environment===scene.environmentTexture.uniqueId;
   for(const mode of ['basecolor','normal','roughness','metallic','ao','opacity','emissive','sss','displacement','material_edges','wireframe','material']){
    stage(mode);
    await prismModel.execute('mode',{mode});
   }
   await prismModel.execute('highlight',{binding_ids:[visible[0].name],duration_ms:50});
   check(visible[0].renderOutline&&!visible[0].material.wireframe,'Selection leaked wireframe');
   await new Promise(resolve=>setTimeout(resolve,100));check(!visible[0].renderOutline,'Selection did not expire');
   camera.target.addInPlace(new B.Vector3(100,100,100));camera.radius*=3;camera.inertialPanningX=5;
   await prismModel.execute('reset',{});
   check(JSON.stringify(prismModel.cameraState())===JSON.stringify(cameraBefore),'Front reset did not restore stable camera');
   camera.alpha+=.3;camera.beta-=.15;const chosen=prismModel.cameraState();
   stage('cover');
   await prismModel.execute('highlight',{binding_ids:[visible[0].name]});
   const cover=await prismModel.execute('cover',{width:256,height:256});
   check(JSON.stringify(chosen)===JSON.stringify(prismModel.cameraState()),'Cover changed camera');
   await prismModel.execute('highlight',{binding_ids:[]});
   const clean=await prismModel.execute('cover',{width:256,height:256});
   check(cover.image===clean.image,'Cover contains selection highlight');
   await prismModel.execute('camera',{camera:chosen});check(JSON.stringify(chosen)===JSON.stringify(prismModel.cameraState()),'Camera state cannot roundtrip');
   stage('thumbnail');
   const thumbnail=await prismModel.execute('thumbnail',{profile:input.scene.profile,material:input.scene.materials[wid]});
   check(thumbnail.image===ballImage,'Model material ball differs from material-library ball');
   check(sameEnvironment,'Environment replaced');check(nonblack>100,'Model is black');
   return {name:input.name,vertices:input.scene.geometry.statistics.points,bindings:first.bindings.length,
    materials:first.resources.materials,visible:visible.length,nonblack,geometryReused:true,texturesReused:true,
    slotIsolation:true,cameraPreserved:true,resetFront:true,coverClean:true,cameraRoundtrip:true,
    identicalMaterialBall:true,sameEnvironment,cover:cover.image,shader:ballShader,resources:second.resources};
  })(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('Audit timeout at '+window.auditStage)),60000))]),input);
  const blob=Buffer.from(result.cover.split(',')[1],'base64');delete result.cover;
  fs.writeFileSync(path.join(out,result.name+'-cover.png'),blob);
  result.originalHashesUnchanged=Object.entries(input.hashes).every(([p,h])=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex')===h);
  assert(result.originalHashesUnchanged);report.cases.push(result);
  fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(result));
 }
 assert.deepStrictEqual(report.errors,[]);report.status='passed';
}catch(error){report.status='failed';report.error=String(error);console.error(error);process.exitCode=1;}
finally{fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));if(browser)await browser.close();}})();
