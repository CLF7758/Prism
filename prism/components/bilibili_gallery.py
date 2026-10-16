"""Adapt public Opus/Dynamic/Article image results from bilibili-api-python."""
from urllib.parse import urlsplit
import re
try:
    from .gallery_media import image_asset
except ImportError:
    from gallery_media import image_asset


def gallery_reference(url):
    parts=urlsplit(url)
    for kind,pattern in (('opus',r'^/opus/(\d+)'),('article',r'^/read/cv(\d+)'),('dynamic',r'^/dynamic/(\d+)')):
        match=re.match(pattern,parts.path)
        if match:return kind,int(match.group(1))
    if parts.hostname=='t.bilibili.com' and parts.path.strip('/').isdigit():
        return 'dynamic',int(parts.path.strip('/'))
    return None


def article_images(value):
    results=[]
    def walk(node):
        if not isinstance(node,dict):return
        if node.get('type')=='ImageNode' and node.get('url'):
            results.append({'url':node['url']})
        for child in node.get('children') or []:walk(child)
    walk(value)
    return results


def dynamic_images(value):
    major=value.get('item',{}).get('modules',{}).get('module_dynamic',{}).get('major') or {}
    opus=major.get('opus') or {}
    if opus.get('pics'):return opus['pics'],opus.get('title','')
    draw=major.get('draw') or {}
    return [dict(v,url=v.get('src')) for v in draw.get('items',[])],''


async def download_gallery(url, credential, request, output):
    import aiohttp
    from bilibili_api import opus,dynamic,article
    kind,identifier=gallery_reference(url)
    title=''
    if kind=='opus':
        work=opus.Opus(identifier,credential=credential)
        pictures=await work.get_images_raw_info()
        info=await work.get_info()
        title=next((m['module_title'].get('text','') for m in info.get('item',{}).get('modules',[]) if m.get('module_title')),'')
    elif kind=='dynamic':
        info=await dynamic.Dynamic(identifier,credential=credential).get_info()
        pictures,title=dynamic_images(info)
    else:
        work=article.Article(identifier,credential=credential)
        await work.fetch_content()
        document=work.json()
        pictures=article_images(document)
        title=document.get('meta',{}).get('title','')
    if not pictures or len(pictures)>100:
        raise ValueError('B站图文未返回可下载图片，或超过单次 100 张上限；不会拿封面替代正文')
    assets=[]
    timeout=aiohttp.ClientTimeout(total=None,sock_connect=20,sock_read=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index,picture in enumerate(pictures):
            target=picture.get('url') or ''
            if target.startswith('//'):target='https:'+target
            host=urlsplit(target).hostname or ''
            if not any(host==d or host.endswith('.'+d) for d in ('hdslb.com','biliimg.com')):
                raise ValueError('B站图文图片地址不在平台媒体域名内')
            path=output/f'bilibili-{identifier}-{index+1:03}.part'
            async with session.get(target,headers={'Referer':'https://www.bilibili.com/'}) as response:
                response.raise_for_status()
                done=0
                with path.open('xb') as stream:
                    async for chunk in response.content.iter_chunked(256*1024):stream.write(chunk);done+=len(chunk)
                if not done or (response.content_length and done!=response.content_length):
                    raise ValueError('B站图文图片下载不完整')
            assets.append(image_asset(request,path,picture))
    return assets,title,None
