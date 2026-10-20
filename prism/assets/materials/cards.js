/* Prism/SurfaceStudio adapter. SurfaceStudio drawCards is kept unmodified. */
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const icon=n=>`<svg aria-hidden="true"><use href="#i-${n}"/></svg>`;
let filtered=[],selected=null,limit=48,state={favorites:[]},compared=[];
let bridge=null,generation=0,assetsById=new Map(),applying=false;
let selectedIds=new Set();
let thumbnailStore=new Map();
function imageURL(asset){return thumbnailStore.get(asset.thumbnail)||'default-sphere.png';}
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
    card.draggable=true;
    card.title='按住左键拖出当前槽位使用的原始贴图；Ctrl 可多选';
    card.classList.toggle('selected',selectedIds.has(card.dataset.asset));
    card.setAttribute('aria-pressed',String(card.dataset.asset===selected));
    const meta=card.querySelector('.card-category');
    const img=card.querySelector('.card-image img');
    img.draggable=false;
    if(img.getAttribute('src')!==imageURL(asset))img.src=imageURL(asset);
    card.querySelector('h3').textContent=asset.name;
    card.querySelector('h3').title=asset.name;
    card.querySelector('.resolution-badge').textContent=asset.resolution;
    meta.textContent=asset.sourceLabel+' · '+asset.fileCount+' 贴图';
    card.querySelector('.card-downloaded').textContent=asset.versionLabel.includes('未提供')?'':asset.versionLabel;
    const tags=document.createElement('div');tags.className='card-tags';
    for(const value of asset.tags.slice(0,3)){
      const span=document.createElement('span');span.textContent=value;tags.append(span);
    }
    card.querySelector('.card-tags')?.remove();card.append(tags);
  });
}

window.prismCards={
  reveal(id){
    const index=filtered.findIndex(asset=>asset.id===id);if(index<0)return;
    if(index>=limit){const start=$$('.asset-card').length;
      limit=Math.min(filtered.length,Math.ceil((index+1)/48)*48);drawCards(true);decorate(start);
    }
    document.querySelector('[data-asset="'+id+'"]')?.scrollIntoView({block:'center'});
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
    const same=filtered.length===data.assets.length&&filtered.every((a,i)=>a.id===data.assets[i].id);
    filtered=data.assets;assetsById=new Map(filtered.map(a=>[a.id,a]));selected=data.selected;
    selectedIds=new Set(data.selectedIds||[selected]);
    limit=Math.max(48,Math.ceil((data.scroll+innerHeight)/180)*8);
    if(!same||!document.querySelector('.asset-card')&&filtered.length)drawCards();
    decorate();
    prismPerformance.span('setData',started);
    prismPerformance.count('records',data.assets.length);
    // No nested animation-frame gate: Chromium can throttle these in an
    // inactive embedded view, leaving scrolling/loading disabled indefinitely.
    window.scrollTo(0,data.scroll);applying=false;requestImages();
  },
  setLayout(mode,size){
    $('#asset-grid').classList.toggle('list-mode',mode==='list');
    document.documentElement.style.setProperty('--card-size',size+'px');
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
  bridge.chooseMany(id,generation,!!(event&&(event.ctrlKey||event.metaKey)));
}
document.addEventListener('click',e=>choose(e.target.closest('[data-asset]'),e));
document.addEventListener('dblclick',e=>{const card=e.target.closest('[data-asset]');if(card&&bridge)bridge.open(card.dataset.asset,generation);});
document.addEventListener('dragstart',e=>{
  const card=e.target.closest('[data-asset]');if(!card||!bridge)return;
  e.preventDefault();bridge.dragTextures(card.dataset.asset,generation);
});
document.addEventListener('contextmenu',e=>{
  const card=e.target.closest('[data-asset]');if(!card||!bridge)return;
  e.preventDefault();document.getElementById('material-menu')?.remove();
  const menu=document.createElement('div');menu.id='material-menu';
  Object.assign(menu.style,{position:'fixed',left:Math.min(e.clientX,innerWidth-190)+'px',top:Math.min(e.clientY,innerHeight-48)+'px',background:'#303136',padding:'6px',border:'1px solid #555',borderRadius:'6px',zIndex:1000});
  const button=document.createElement('button');button.textContent=selectedIds.has(card.dataset.asset)&&selectedIds.size>1?'导出选中的 '+selectedIds.size+' 个材质…':'导出原始贴图…';
  button.style.cssText='background:transparent;color:#eee;border:0;padding:8px 16px;cursor:pointer';
  button.onclick=()=>{bridge.export(card.dataset.asset,generation);menu.remove();};
  menu.append(button);document.body.append(menu);
  document.addEventListener('click',()=>menu.remove(),{once:true});
});
document.addEventListener('keydown',e=>{
  const card=e.target.closest('[data-asset]');if(!card)return;
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
  if(innerHeight+scrollY>=document.documentElement.scrollHeight-350&&limit<filtered.length){
    const started=performance.now(),start=$$('.asset-card').length;
    limit+=48;drawCards(true);decorate(start);
    prismPerformance.span('scroll_append',started);
  }
  clearTimeout(scrollTimer);
  scrollTimer=setTimeout(()=>{if(bridge)bridge.scrolled(Math.round(scrollY),generation);},60);
});
new QWebChannel(qt.webChannelTransport,channel=>{bridge=channel.objects.materialCards;
  prismPerformance.connect(value=>bridge.metrics(value));bridge.ready();});
