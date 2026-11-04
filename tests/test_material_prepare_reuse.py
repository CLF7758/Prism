import hashlib
import io
from PIL import Image
import prism.material_textures as mt


def test_single_prepare_decodes_unique_sources_once_without_cross_call_cache(monkeypatch,tmp_path):
    blobs={}
    for key,color in [(1,(200,100,50,128)),(2,(128,128,255,255)),(3,(25,125,225,255))]:
        stream=io.BytesIO();Image.new('RGBA',(8,8),color).save(stream,format='PNG');blobs[key]=stream.getvalue()
    attachments={key:('source','image/png',blob) for key,blob in blobs.items()}
    record={'id':'m','textures':{slot:dict(attachmentId=key,sha256=hashlib.sha256(blobs[key]).hexdigest(),channel=channel,name=slot)
        for slot,key,channel in [('basecolor',1,'RGB'),('normal',2,'RGB'),('ao',3,'R'),('roughness',3,'G'),('metallic',3,'B')]}}
    monkeypatch.setattr(mt,'_DECODED_TEXTURES',mt.DecodedTextureCache(budget=0))
    opened=[];original_open=Image.open
    def counted(*a,**kw):
        opened.append(1);return original_open(*a,**kw)
    with monkeypatch.context() as patch:
        patch.setattr(Image,'open',counted)
        payload=mt.prepare_render(record,attachments,tmp_path)
    assert len(opened)==3
    assert 'metallicRoughness' in payload['maps']
    with original_open(tmp_path/'metallicRoughness.png') as packed:
        assert packed.getpixel((0,0))==(255,125,225)
    with original_open(tmp_path/'opacity.png') as opacity:
        assert opacity.getpixel((0,0))==128
