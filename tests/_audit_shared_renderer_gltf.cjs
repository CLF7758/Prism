// Compare the production adapter to the shipped official glTF loader.
const fs=require('fs'),path=require('path'),{pathToFileURL}=require('url');
const {chromium}=require('D:/AITest/node_modules/playwright-core');
const root=path.resolve(__dirname,'..'),out=path.join(root,'artifacts/shared-renderer-integration-20261102/gltf-reference');
const input=JSON.parse(fs.readFileSync(path.join(out,'input.json'),'utf8'));
(async()=>{let browser;try{
 browser=await chromium.launch({headless:true,executablePath:'C:/Users/admin/AppData/Local/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe',args:['--allow-file-access-from-files','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 const page=await browser.newPage({viewport:{width:512,height:512}});
 await page.addInitScript(()=>{window.qt={webChannelTransport:{}};window.QWebChannel=function(_,callback){callback({objects:{materialRender:{ready(){window.bridgeReady=true},metrics(){},model_result(){},model_event(){},updated(){},frame_submitted(){},rendered(){},loaded(){},error(){}}}})};});
 await page.goto(pathToFileURL(path.join(root,'prism/assets/materials/render.html')).href);
 await page.waitForFunction(()=>window.bridgeReady&&window.prismModel);
 const report=await page.evaluate(async input=>{
  await prismModel.load(input.scene);scene.render();
  const pixels=await engine.readPixels(0,0,512,512);
  const mesh=scene.meshes.find(m=>m.metadata?.binding),mat=mesh.material;
  const describe=m=>({serialized:m.serialize(),textureMatrix:m.albedoTexture.getTextureMatrix().asArray()});
  const geometry=m=>({orientation:m.overrideMaterialSideOrientation,side:m.material.sideOrientation,indices:Array.from(m.getIndices()),data:Object.fromEntries(m.getVerticesDataKinds().map(k=>[k,Array.from(m.getVerticesData(k))]))});
  const adapted={geometry:geometry(mesh),detail:describe(mat),invertX:mat.invertNormalMapX,invertY:mat.invertNormalMapY,textureInvertY:mat.albedoTexture.invertY,
   rotation:mat.albedoTexture.wAng,world:mesh.getWorldMatrix().asArray()};
  prismModel.release();
  const container=await B.SceneLoader.LoadAssetContainerAsync('',input.source,scene);container.addAllToScene();wake();
  const conversion=container.meshes.find(m=>m.name==='__root__');
  const loaderRotation=conversion?.rotationQuaternion?.asArray();
  // The model library preserves source +Z front; the official viewer adds a
  // 180-degree presentation yaw. Compare identical world geometry, not opposite views.
  if(conversion){conversion.rotationQuaternion=B.Quaternion.Identity();conversion.computeWorldMatrix(true);}
  const official=container.meshes.find(m=>m.material)?.material;
  // Keep the official loader's source UV/normal/material interpretation, but
  // use the shared viewer's sampling and PBR quality settings on both sides.
  official.enableSpecularAntiAliasing=mat.enableSpecularAntiAliasing;
  official.forceIrradianceInFragment=mat.forceIrradianceInFragment;
  const officialAlbedo=official.albedoTexture;
  const referenceAlbedo=await texture(input.scene.materials[Object.keys(input.scene.materials)[0]].maps.basecolor,true,false);
  for(const key of ['uOffset','vOffset','uScale','vScale','wAng','uRotationCenter','vRotationCenter'])referenceAlbedo[key]=officialAlbedo[key];
  official.albedoTexture=referenceAlbedo;
  for(const mesh of container.meshes)if(mesh.material)await mesh.material.forceCompilationAsync(mesh);
  await scene.whenReadyAsync();scene.render();
  const reference=await engine.readPixels(0,0,512,512);
  let max=0,changed=0,total=0;
  for(let i=0;i<pixels.length;i++){const diff=Math.abs(pixels[i]-reference[i]);max=Math.max(max,diff);total+=diff;if(diff>1)changed++;}
  return {adapted,loaderPresentationRotation:loaderRotation,official:{geometry:geometry(container.meshes.find(m=>m.material)),detail:describe(official),invertX:official.invertNormalMapX,invertY:official.invertNormalMapY,
   textureInvertY:official.albedoTexture.invertY,rotation:official.albedoTexture.wAng,
   world:container.meshes.find(m=>m.material).getWorldMatrix().asArray()},
   maxChannelDifference:max,meanChannelDifference:total/pixels.length,channelsOverOne:changed};
 },input);
 report.status=report.maxChannelDifference<=1?'passed':'failed';
 fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({status:report.status,max:report.maxChannelDifference,mean:report.meanChannelDifference}));
 if(report.status!=='passed')process.exitCode=1;
}catch(error){console.error(error);process.exitCode=1;}finally{if(browser)await browser.close();}})();
