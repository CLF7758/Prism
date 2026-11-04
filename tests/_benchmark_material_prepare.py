"""Read-only real-asset A/B; no GUI or user project mutation."""
import sys, json, time, tempfile, hashlib, threading, statistics, platform
from pathlib import Path
import ctypes
from ctypes import wintypes
class MemoryCounters(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in ['peak_working','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile']]
def rss():
    counters=MemoryCounters();counters.cb=ctypes.sizeof(counters)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(-1),ctypes.byref(counters),ctypes.sizeof(counters)):
        raise ctypes.WinError()
    return counters.working
from PIL import Image
import prism.material_textures as mt

root=Path('artifacts/material-performance-20261029')
assets=Path('E:/Prism-stress-100k/Game/LZPV/Assets/Envs/Slay/Assets/SetDec/WoodCarving')
blobs={i:(assets/name).read_bytes() for i,name in [(1,'T_WoodCarving_BC.PNG'),(2,'T_WoodCarving_N.PNG'),(3,'T_WoodCarving_ORM.PNG')]}
attachments={i:(str(i),'image/png',b) for i,b in blobs.items()}
record=dict(id='real-woodcarving',textures={slot:dict(attachmentId=i,sha256=hashlib.sha256(blobs[i]).hexdigest(),channel=ch,name=str(i)) for slot,i,ch in [('basecolor',1,'RGB'),('normal',2,'RGB'),('roughness',3,'G'),('metallic',3,'B'),('ao',3,'R')]},preview={'properties':{}})
namespace=dict(mt.__dict__)
exec((root/'prepare_render_before.py').read_text(encoding='utf-8'),namespace)
old=namespace['prepare_render']

open_image=Image.open
samples=[]
# Disable cross-call cache in BOTH variants to isolate within-call reuse.
mt._DECODED_TEXTURES.clear()
mt._DECODED_TEXTURES=mt.DecodedTextureCache(budget=0)
for iteration in range(5):
 for label,fn in ([('before',old),('after',mt.prepare_render)] if iteration%2==0 else [('after',mt.prepare_render),('before',old)]):
    counts={'source_decodes':0}
    def counted(*args,**kwargs):
        counts['source_decodes']+=1
        return open_image(*args,**kwargs)
    before=rss()
    peak=[before];stop=threading.Event()
    def poll():
        while not stop.wait(.01):peak[0]=max(peak[0],rss())
    thread=threading.Thread(target=poll);thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            Image.open=counted
            t=time.perf_counter()
            payload=fn(record,attachments,directory)
            ms=(time.perf_counter()-t)*1000
            Image.open=open_image
            stop.set();thread.join()
            peak[0]=max(peak[0],rss())
            pixels={}
            for path in sorted(Path(directory).glob('*.png')):
                with open_image(path) as image:
                    pixels[path.name]=dict(size=list(image.size),mode=image.mode,sha256=hashlib.sha256(image.tobytes()).hexdigest())
            samples.append(dict(variant=label,iteration=iteration,ms=ms,rss_before=before,sampled_peak_rss=peak[0],peak_delta=peak[0]-before,pixels=pixels,**counts))
    finally:
        Image.open=open_image;stop.set();thread.join()
reference=samples[0]['pixels']
assert all(s['pixels']==reference for s in samples),'Pixel mismatch'
summary={}
for label in ('before','after'):
    rows=[s for s in samples if s['variant']==label]
    times=[s['ms'] for s in rows]
    summary[label]=dict(n=len(times),p50_ms=statistics.median(times),p95_ms=statistics.quantiles(times,n=100,method='inclusive')[94],max_ms=max(times),source_decodes=[s['source_decodes'] for s in rows],max_peak_rss=max(s['sampled_peak_rss'] for s in rows),max_peak_delta=max(s['peak_delta'] for s in rows))
assert all((assets/name).read_bytes()==blobs[i] for i,name in [(1,'T_WoodCarving_BC.PNG'),(2,'T_WoodCarving_N.PNG'),(3,'T_WoodCarving_ORM.PNG')])
result=dict(summary=summary,samples=samples,pixels_equal=True,originals_unchanged=True,cross_call_cache_budget=0,condition='Five alternating runs per variant; shared process, warm OS cache; RSS sampled every 10ms, not a leak test; p95 descriptive for small n, not interaction SLO',python=sys.version,platform=platform.platform(),source_hashes={str(i):hashlib.sha256(b).hexdigest() for i,b in blobs.items()},function_hashes={f:hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in ['prism/material_textures.py',str(root/'prepare_render_before.py')]})
(root/'prepare_render_ab.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(summary,indent=2))
