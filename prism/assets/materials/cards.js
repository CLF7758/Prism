/* Prism/SurfaceStudio adapter. SurfaceStudio drawCards is kept unmodified. */
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const icon=n=>`<svg aria-hidden="true"><use href="#i-${n}"/></svg>`;
let filtered=[],selected=null,limit=48,state={favorites:[]},compared=[];
let bridge=null,generation=0,assetsById=new Map(),applying=false;
let selectedIds=new Set();
let thumbnailStore=new Map();
// The model library pages through a larger library. It installs a bounded
// window of summaries and asks for the next page instead of letting the DOM
// grow without limit. The material library keeps the historic append mode.
let bounded=false,needsMorePending=false;
let virtual=false,virtualStep=0,virtualFrame=0;
function renderVirtual(){
  if(!virtual)return;
  const grid=$('#asset-grid'),all=filtered;
  const columns=Math.max(1,getComputedStyle(grid).gridTemplateColumns.split(' ').length);
  const previous=grid.querySelector('.asset-card');
  const step=previous?previous.getBoundingClientRect().height+(parseFloat(getComputedStyle(grid).rowGap)||0):(virtualStep||220);
  const first=Math.max(0,Math.floor(scrollY/step)-2)*columns;
  const last=Math.min(all.length,first+(Math.ceil(innerHeight/step)+5)*columns);
  filtered=all.slice(first,last);limit=filtered.length;
  try{drawCards();decorate();}finally{filtered=all;}
  const card=grid.querySelector('.asset-card');
  if(card)virtualStep=card.getBoundingClientRect().height+(parseFloat(getComputedStyle(grid).rowGap)||0);
  grid.style.paddingTop=(first/columns*(virtualStep||step))+'px';
  grid.style.paddingBottom=(Math.max(0,Math.ceil(all.length/columns)-Math.ceil(last/columns))*(virtualStep||step))+'px';
  const keep=new Set($$('.asset-card').map(card=>assetsById.get(card.dataset.asset)?.thumbnail));
  for(const key of thumbnailStore.keys()){
    if(thumbnailStore.size<=128)break;
    if(!keep.has(key))thumbnailStore.delete(key);
  }
}
function modelPlaceholder(){
  return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="355">'+
    '<rect width="480" height="355" fill="#222225"/>'+
    '<path d="M196 118h88v74h-88z m0 74 22-26 22 21 16-13 28 24" fill="none" stroke="#68717d" stroke-width="2"/>'+
    '<text x="240" y="245" text-anchor="middle" fill="#929ba8" font-size="16" '+
    'font-family="Microsoft YaHei,sans-serif">模型封面待生成</text></svg>');
}
function imageURL(asset){
  if(thumbnailStore.has(asset.thumbnail)&&thumbnailStore.get(asset.thumbnail))return thumbnailStore.get(asset.thumbnail);
  if(asset.assetKind==='model')return modelPlaceholder();
  if(asset.assetKind&&asset.assetKind!=='material')return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160"><rect width="240" height="160" fill="#222225"/></svg>');
  return 'default-sphere.png';
}
let imageRequestPending=false,imageTimer;
function requestImages(){
  if(!bridge||imageRequestPending)return;
  const ids=$$('.asset-card').filter(card=>{
    const asset=assetsById.get(card.dataset.asset),rect=card.getBoundingClientRect();
    return asset&&!thumbnailStore.has(asset.thumbnail)&&rect.bottom>=-100&&rect.top<=innerHeight+200;
  }).slice(0,16).map(card=>card.dataset.asset);
  if(ids.length){imageRequestPending=true;bridge.imagesRequested(JSON.stringify(ids),generation);}
}
function updateImages(){
  for(const card of $$('.asset-card')){
    const asset=assetsById.get(card.dataset.asset),img=card.querySelector('.card-image img');
    if(asset&&img&&img.getAttribute('src')!==imageURL(asset))img.src=imageURL(asset);
  }
}

function decorate(start=0){
  $$('.asset-card').slice(start).forEach(card=>{
    const asset=assetsById.get(card.dataset.asset);
    if(!asset)return;
    // Qt owns library moves. Chromium's native drag must not start a
    // second, nested OS drag loop while WebChannel is asking Qt to drag.
    card.draggable=false;
    card.querySelectorAll('img').forEach(img=>img.draggable=false);
    card.title='按住左键拖出当前槽位使用的原始贴图；Ctrl 可多选';
    if(asset.assetKind==='model'){
      card.title=asset.previewEnabled===false
        ?'单击查看资料；交互预览尚未接入'
        :'单击查看资料，双击进入大预览；Ctrl 可多选';
    }
    card.classList.toggle('selected',selectedIds.has(card.dataset.asset));
    card.setAttribute('aria-pressed',String(card.dataset.asset===selected));
    const meta=card.querySelector('.card-category');
    const img=card.querySelector('.card-image img');
    img.draggable=false;
    if(asset.assetKind==='model')img.alt=asset.name+' · 模型封面';
    if(img.getAttribute('src')!==imageURL(asset))img.src=imageURL(asset);
    card.querySelector('h3').textContent=asset.name;
    card.querySelector('h3').title=asset.name;
    card.querySelector('.resolution-badge').textContent=asset.resolution;
    meta.textContent=asset.metaLabel??(asset.sourceLabel+' · '+asset.fileCount+' 贴图');
    card.querySelector('.card-downloaded').textContent=asset.versionLabel.includes('未提供')?'':asset.versionLabel;
    const tags=document.createElement('div');tags.className='card-tags';
    for(const value of asset.tags.slice(0,3)){
      const span=document.createElement('span');span.textContent=value;tags.append(span);
    }
    card.querySelector('.card-tags')?.remove();card.append(tags);
  });
}

window.prismCards={
  appendData(data){
    generation=data.generation;imageRequestPending=false;
    for(const asset of data.assets){filtered.push(asset);assetsById.set(asset.id,asset);}
    renderVirtual();requestImages();
  },
  reveal(id){
    const index=filtered.findIndex(asset=>asset.id===id);if(index<0)return;
    if(virtual){
      const columns=Math.max(1,getComputedStyle($('#asset-grid')).gridTemplateColumns.split(' ').length);
      const top=Math.floor(index/columns)*(virtualStep||220),bottom=top+(virtualStep||220);
      if(top<scrollY)window.scrollTo(0,top);
      else if(bottom>scrollY+innerHeight)window.scrollTo(0,bottom-innerHeight);
      renderVirtual();requestImages();return;
    }
    if(index>=limit){const start=$$('.asset-card').length;
      limit=Math.min(filtered.length,Math.ceil((index+1)/48)*48);drawCards(true);decorate(start);
    }
    document.querySelector('[data-asset="'+id+'"]')?.scrollIntoView({block:'nearest'});
  },
  receiveImages(images,version){
    if(version!==generation)return;
    for(const [id,url] of Object.entries(images))thumbnailStore.set(id,url);
    imageRequestPending=false;updateImages();requestImages();
  },
  updateAsset(asset,images={}){
    for(const [id,url] of Object.entries(images))thumbnailStore.set(id,url);
    const old=assetsById.get(asset.id);if(!old)return;
    Object.assign(old,asset);
    const card=document.querySelector('[data-asset="'+asset.id+'"]');
    if(card){const img=card.querySelector('.card-image img');if(img)img.src=imageURL(asset);}
    prismPerformance.count('card_patches');
  },
  setData(data){
    const started=performance.now();
    const needed=new Set(data.assets.map(asset=>asset.thumbnail));
    for(const id of thumbnailStore.keys())if(!needed.has(id))thumbnailStore.delete(id);
    imageRequestPending=false;
    applying=true;generation=data.generation;
    bounded=data.bounded===true;
    virtual=data.virtual===true;
    if(!virtual){$('#asset-grid').style.paddingTop='';$('#asset-grid').style.paddingBottom='';}
    needsMorePending=false;
    const same=filtered.length===data.assets.length&&filtered.every((a,i)=>a.id===data.assets[i].id);
    filtered=data.assets;assetsById=new Map(filtered.map(a=>[a.id,a]));selected=data.selected;
    selectedIds=new Set(data.selectedIds||[selected]);
    limit=Math.max(48,Math.ceil((data.scroll+innerHeight)/180)*8);
    if(virtual)renderVirtual();
    else{if(!same||!document.querySelector('.asset-card')&&filtered.length)drawCards();decorate();}
    prismPerformance.span('setData',started);
    prismPerformance.count('records',data.assets.length);
    // No nested animation-frame gate: Chromium can throttle these in an
    // inactive embedded view, leaving scrolling/loading disabled indefinitely.
    window.scrollTo(0,data.scroll);applying=false;requestImages();
  },
  setLayout(mode,size){
    $('#asset-grid').classList.toggle('list-mode',mode==='list');
    document.documentElement.style.setProperty('--card-size',size+'px');
    if(virtual){virtualStep=0;renderVirtual();requestImages();}
  },
  setSelection(id,ids){
    selected=id;
    selectedIds=new Set(ids||[id]);
    $$('.asset-card').forEach(card=>{
      const match=selectedIds.has(card.dataset.asset);card.classList.toggle('selected',match);
      card.setAttribute('aria-pressed',String(match));
    });
  },
  scrollTo(value){window.scrollTo(0,value);}
};
function choose(card,event){
  if(!card||!bridge)return;
  const id=card.dataset.asset;
  if(event?.shiftKey){bridge.chooseRange(id,generation,!!(event.ctrlKey||event.metaKey));return;}
  bridge.chooseMany(id,generation,!!(event&&(event.ctrlKey||event.metaKey)));
}
function beginRename(card){
  const asset=assetsById.get(card.dataset.asset), label=card.querySelector('h3');
  if(!asset||asset.renameEnabled===false||!label||label.querySelector('input'))return;
  closeCardMenu();
  const original=asset.name, input=document.createElement('input'), version=generation;
  input.value=original;input.maxLength=160;
  input.style.cssText='width:100%;box-sizing:border-box;background:#222;color:white;border:1px solid #70aaff;';
  label.replaceChildren(input);let done=false;
  function finish(cancel){if(done)return;done=true;const name=input.value.trim();label.textContent=original; // service refresh acknowledges the accepted name
    if(!cancel&&name&&name!==original&&version===generation)bridge.renameInline(card.dataset.asset,name,version);}
  input.addEventListener('click',e=>e.stopPropagation());
  input.addEventListener('dblclick',e=>e.stopPropagation());
  input.addEventListener('keydown',e=>{e.stopPropagation();if(e.key==='Enter'){e.preventDefault();finish(false);}if(e.key==='Escape'){e.preventDefault();finish(true);}});
  input.addEventListener('blur',()=>finish(false));input.focus();input.select();
}
function closeCardMenu(){document.getElementById('material-menu')?.remove();}
let libraryDrag=null,suppressDragClick=false;
document.addEventListener('pointerdown',e=>{
  libraryDrag=null;suppressDragClick=false;
  const card=e.target.closest('[data-asset]');
  if(e.button!==0||!card||e.target.closest('input,button,a')||assetsById.get(card.dataset.asset)?.dragEnabled===false)return;
  libraryDrag={id:card.dataset.asset,x:e.clientX,y:e.clientY,version:generation};
});
document.addEventListener('pointermove',e=>{
  const drag=libraryDrag;if(!drag)return;
  if(!(e.buttons&1)||drag.version!==generation){libraryDrag=null;return;}
  if(Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<8)return;
  libraryDrag=null;suppressDragClick=true;e.preventDefault();
  bridge?.dragTextures(drag.id,drag.version);
});
document.addEventListener('pointerup',()=>{libraryDrag=null;});
document.addEventListener('pointercancel',()=>{libraryDrag=null;});
document.addEventListener('click',e=>{
  if(suppressDragClick){suppressDragClick=false;e.preventDefault();return;}
  choose(e.target.closest('[data-asset]'),e);
  closeCardMenu();
});
window.addEventListener('blur',closeCardMenu);
document.addEventListener('dblclick',e=>{const card=e.target.closest('[data-asset]');if(!card||!bridge)return;if(e.target.closest('h3')){beginRename(card);return;}bridge.open(card.dataset.asset,generation);});
document.addEventListener('dragstart',e=>{
  const card=e.target.closest('[data-asset]');if(!card||!bridge)return;
  if(assetsById.get(card.dataset.asset)?.dragEnabled===false){e.preventDefault();return;}
  e.preventDefault();bridge.dragTextures(card.dataset.asset,generation);
});
document.addEventListener('contextmenu',e=>{
  const card=e.target.closest('[data-asset]');if(!card||!bridge)return;
  const asset=assetsById.get(card.dataset.asset);
  if(asset?.exportEnabled===false&&!asset?.restoreEnabled){e.preventDefault();return;}
  e.preventDefault();document.getElementById('material-menu')?.remove();
  const menu=document.createElement('div');menu.id='material-menu';
  Object.assign(menu.style,{position:'fixed',left:Math.min(e.clientX,innerWidth-190)+'px',top:Math.min(e.clientY,innerHeight-48)+'px',background:'#303136',padding:'6px',border:'1px solid #555',borderRadius:'6px',zIndex:1000});
  const isModel=asset?.assetKind==='model';
  const many=selectedIds.has(card.dataset.asset)&&selectedIds.size>1;
  const button=document.createElement('button');
  button.textContent=asset?.restoreEnabled?'恢复到原位置':isModel
    ?(many?'导出选中的 '+selectedIds.size+' 个模型原件…':'导出模型原件…')
    :(many?'导出选中的 '+selectedIds.size+' 个材质…':'导出原始贴图…');
  button.style.cssText='background:transparent;color:#eee;border:0;padding:8px 16px;cursor:pointer';
  button.onclick=()=>{if(asset?.restoreEnabled)bridge.restore(card.dataset.asset,generation);else bridge.export(card.dataset.asset,generation);menu.remove();};
  menu.append(button);document.body.append(menu);
  if(asset?.renameEnabled!==false){
    const rename=document.createElement('button');rename.textContent='重命名 (F2)';
    rename.style.cssText=button.style.cssText+';display:block';
    rename.onclick=()=>{beginRename(card);menu.remove();};
    menu.append(rename);
  }
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&document.getElementById('material-menu')){
    e.preventDefault();e.stopPropagation();closeCardMenu();return;
  }
  if(e.target.closest('input,textarea,[contenteditable="true"]'))return;
  if(e.key==='Delete'&&bridge){e.preventDefault();bridge.deleteSelected(generation);return;}
  const card=e.target.closest('[data-asset]');if(!card)return;
  if(e.key==='F2'&&bridge){e.preventDefault();beginRename(card);return;}
  if(e.key==='Enter'||e.key===' '){e.preventDefault();choose(card);return;}
  if(['ArrowRight','ArrowDown','ArrowLeft','ArrowUp'].includes(e.key)){
    const cards=$$('.asset-card'),index=cards.indexOf(card);
    const columns=getComputedStyle($('#asset-grid')).gridTemplateColumns.split(' ').length;
    const delta=e.key==='ArrowDown'?columns:e.key==='ArrowUp'?-columns:e.key==='ArrowRight'?1:-1;
    const next=cards[index+delta];if(next){e.preventDefault();next.focus();choose(next);}
  }
});
let scrollTimer;
window.addEventListener('scroll',()=>{
  clearTimeout(imageTimer);imageTimer=setTimeout(requestImages,16);
  if(applying)return;
  if(virtual){
    cancelAnimationFrame(virtualFrame);
    virtualFrame=requestAnimationFrame(()=>{renderVirtual();requestImages();});
    clearTimeout(scrollTimer);
    scrollTimer=setTimeout(()=>{if(bridge)bridge.scrolled(Math.round(scrollY),generation);},60);
    return;
  }
  const atEnd=innerHeight+scrollY>=document.documentElement.scrollHeight-350;
  if(atEnd&&limit<filtered.length){
    const started=performance.now(),start=$$('.asset-card').length;
    limit+=48;drawCards(true);decorate(start);
    prismPerformance.span('scroll_append',started);
  }
  // Bounded mode: the surface holds one window of summaries. When the user
  // reaches the end of it, the owner loads the next page instead of the DOM
  // growing without limit.
  if(bounded&&!needsMorePending&&atEnd&&limit>=filtered.length&&bridge){
    needsMorePending=true;bridge.needsMore(generation);
  }
  clearTimeout(scrollTimer);
  scrollTimer=setTimeout(()=>{if(bridge)bridge.scrolled(Math.round(scrollY),generation);},60);
});
window.addEventListener('resize',()=>{if(virtual){virtualStep=0;renderVirtual();requestImages();}});
new QWebChannel(qt.webChannelTransport,channel=>{bridge=channel.objects.materialCards;
  prismPerformance.connect(value=>bridge.metrics(value));bridge.ready();});
