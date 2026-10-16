"""Adapters for pinned F2, XHS-Downloader and bilibili-api-python.

Extraction belongs to upstream libraries. This process only translates their
results/progress into Prism's bounded JSON protocol; no Qt. Site sessions are
accepted only from an explicitly supplied cookie file, never auto-discovered.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit, parse_qs
try:
    from .download_result import complete, complete_assets
    from .gallery_media import image_asset, stream_file
    from .xhs_guest import create_guest_cookie
    from .bilibili_gallery import gallery_reference, download_gallery
    from .bilibili_quality import select_streams, enforce_quality, enforce_selected, QualityLimitedError
    from .platform_links import douyin_identifier, xhs_destination, xhs_page_failure, xhs_request_cookie, site_request_cookies
except ImportError:
    from download_result import complete, complete_assets
    from gallery_media import image_asset, stream_file
    from xhs_guest import create_guest_cookie
    from bilibili_gallery import gallery_reference, download_gallery
    from bilibili_quality import select_streams, enforce_quality, enforce_selected, QualityLimitedError
    from platform_links import douyin_identifier, xhs_destination, xhs_page_failure, xhs_request_cookie, site_request_cookies

PROTOCOL = sys.stdout


def emit(event, **fields):
    PROTOCOL.write(json.dumps(dict(event=event, **fields), ensure_ascii=False)+'\n')
    PROTOCOL.flush()


class Quiet:
    def write(self, text):
        return len(text)
    def flush(self):
        pass
    def isatty(self):
        return False


class Progress:
    def __init__(self):
        self.last = 0

    def __call__(self, done, total=None, **fields):
        now = time.monotonic()
        if now-self.last >= .2 or (total and done >= total):
            self.last = now
            emit('progress', phase='download', downloaded=done, total=total, **fields)


def safe(error):
    text = re.sub(r'https?://[^\s\"<>]+', '[链接已隐藏]', str(error))
    text = re.sub(r'(?i)(cookie|mstoken|ttwid|token|authorization)\s*[:=]\s*[^\s,;]+', r'\1=[隐藏]', text)
    return text[-1000:] or type(error).__name__


def probe(request, path, expected=None):
    emit('progress', phase='verify')
    result = subprocess.run([str(Path(request['ffmpeg'])/'ffprobe.exe'), '-v', 'error',
        '-show_entries', 'format=duration:stream=codec_type,codec_name,width,height', '-of', 'json', str(path)],
        capture_output=True, encoding='utf-8', timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    data = json.loads(result.stdout or '{}')
    duration = float(data.get('format', {}).get('duration', 0))
    if result.returncode or duration <= 0 or not any(s.get('codec_type') == 'video' for s in data.get('streams', [])):
        raise ValueError('下载文件未通过视频完整性检查')
    if expected and abs(duration-float(expected)) > max(2, float(expected)*.02):
        raise ValueError('下载视频时长与源视频不符')
    return data


def douyin(request, output):
    from curl_cffi import requests
    from f2.apps.douyin.utils import ClientConfManager, TokenManager, AwemeIdFetcher, get_video_play_urls
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import PostDetail
    from f2.apps.douyin.filter import PostDetailFilter
    from f2.apps.douyin.api import DouyinAPIEndpoints
    with requests.Session(impersonate='chrome', verify=True) as session:
        identifier = douyin_identifier(request['url'], session, AwemeIdFetcher)
        conf = ClientConfManager.ttwid()
        if not urlsplit(conf['url']).hostname.endswith('.bytedance.com'):
            raise ValueError('访客服务地址异常')
        response = session.post(conf['url'], data=conf['data'], headers=TokenManager.ttwid_headers, timeout=20)
        response.raise_for_status()
        ttwid = response.cookies.get('ttwid')
        conf = TokenManager.token_conf
        if not urlsplit(conf['url']).hostname.endswith('.bytedance.com'):
            raise ValueError('访客服务地址异常')
        payload = {k: conf[k] for k in ('magic', 'version', 'dataType', 'strData', 'ulr')}
        payload['tspFromClient'] = int(time.time()*1000)
        response = session.post(conf['url'], data=json.dumps(payload), headers=TokenManager.mstoken_headers, timeout=20)
        response.raise_for_status()
        token = response.cookies.get('msToken')
        if not ttwid or not token or len(token) not in (164, 184):
            raise ValueError('平台未建立可用的匿名访客会话')
        headers = dict(ClientConfManager.headers())
        cookie = 'ttwid='+ttwid+'; msToken='+token
        crawler = DouyinCrawler(dict(headers=headers, cookie=cookie, max_retries=1, timeout=12, verify=True))
        params = PostDetail(aweme_id=identifier, msToken=token)
        endpoint = crawler.bogus_manager.model_2_endpoint(headers.get('User-Agent'), DouyinAPIEndpoints.POST_DETAIL, params.model_dump())
        response = session.get(endpoint, headers=crawler.headers, timeout=25)
        response.raise_for_status()
        payload = response.json()
        post = PostDetailFilter(payload)
        detail = payload.get('aweme_detail') or {}
        if str(post.aweme_id) == identifier and detail.get('images'):
            images = detail['images']
            if len(images)>50:
                raise ValueError('图文图片数量超过单次上限 50 张')
            assets=[]
            progress=Progress()
            image_urls=post.images
            for index, image in enumerate(images):
                emit('progress',phase='download',asset=index+1,assets=len(images))
                path=stream_file(session,[image_urls[index]],output/f'douyin-{identifier}-{index+1:03}-image.part',headers,progress)
                assets.append(image_asset(request,path,image))
                live=image.get('video') or {}
                urls=get_video_play_urls(live) if live else []
                if urls:
                    path=stream_file(session,urls,output/f'douyin-{identifier}-{index+1:03}-live.part',headers,progress)
                    media=probe(request,path,(live.get('duration') or 0)/1000)
                    final=path.with_suffix('.mp4');path.rename(final)
                    assets.append(dict(asset_type='video',path=str(final),media=media,live_photo=True))
            return assets, post.desc, None
        if str(post.aweme_id) != identifier or not post.video_play_addr:
            raise ValueError('平台没有返回可下载的公开视频')
        path = output/('douyin-'+identifier+'.mp4')
        partial = path.with_suffix('.mp4.part')
        response = session.get(post.video_play_addr[0], headers=headers, stream=True, timeout=300)
        progress = Progress()
        try:
            response.raise_for_status()
            total = int(response.headers.get('Content-Length', '0')) or None
            written = 0
            with partial.open('xb') as stream:
                for chunk in response.iter_content(256*1024):
                    if chunk:
                        stream.write(chunk)
                        written += len(chunk)
                        progress(written, total)
            if total and written != total:
                raise ValueError('视频传输不完整，请重试')
        finally:
            response.close()
        media = probe(request, partial, (post.duration or 0)/1000)
        partial.rename(path)
        return path, post.desc, media


async def xiaohongshu(request, output):
    sys.path.insert(0, str(Path(request['platform_root'])/'upstream-source'))
    from source import XHS
    from source.application.download import Download
    # Keep upstream's streamed downloader, with a modest per-job buffer.
    Download.WRITE_BUFFER_SIZE = 1024*1024
    progress = Progress()
    def callback(data):
        progress(data.get('completed_bytes', 0), data.get('total_bytes'))
    cookie = xhs_request_cookie(request)
    if not cookie:
        emit('progress', phase='session')
        cookie = create_guest_cookie()
    async with XHS(work_path=str(output), folder_name='media', cookie=cookie, timeout=20,
        max_retry=0, image_download=True, video_download=True, video_cover_download=False,
        live_download=True, record_data=False, download_record=False, author_archive=False,
        write_mtime=False, script_server=False, image_format='auto', video_preference='resolution') as tool:
        content_kind = []
        upstream_convert = tool.convert.run
        def checked_convert(content):
            result = upstream_convert(content)
            if not result and content:
                # Use upstream parsing, persisting no HTML or embedded tokens.
                state = tool.convert._convert_object(tool.convert._extract_object(content))
                reason = xhs_page_failure(state)
                if reason:
                    raise ValueError(reason)
            if result:
                content_kind[:] = [result.get('type')]
                if result.get('type') != 'video' and len(result.get('imageList') or [])>50:
                    raise ValueError('图文图片数量超过单次上限 50 张')
            return result
        tool.convert.run = checked_convert
        url = request['url']
        if urlsplit(url).hostname in ('xhslink.cn', 'xhslink.com'):
            url = await tool.html.request_url(url, False)
        if not url:
            raise ValueError('小红书短链接请求失败，尚未取得作品地址')
        url, platform_message = xhs_destination(url)
        result = await tool.extract(url, download=True, check_record=False, progress_callback=callback)
    if content_kind and content_kind[0] != 'video':
        files=sorted(p for p in output.rglob('*') if p.is_file() and p.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.avif','.heic','.mp4'))
        if not result or not result[0] or not files:
            raise ValueError('图文作品没有完整下载，未导入空结果')
        assets=[]
        for path in files:
            if path.suffix.lower()=='.mp4':
                assets.append(dict(asset_type='video',path=str(path),media=probe(request,path),live_photo=True))
            else:
                assets.append(image_asset(request,path))
        return assets, result[0].get('作品标题',''), None
    files = list(output.rglob('*.mp4'))
    if len(files) != 1 or not result or not result[0]:
        if platform_message:
            raise ValueError('小红书未向下载器返回视频正文，本次尚未下载成功；不能据此判断原链接失效。')
        raise ValueError('未下载到完整视频；链接可能不是视频或受到平台访问限制')
    media = probe(request, files[0])
    return files[0], result[0].get('作品标题', ''), media


async def bilibili(request, output):
    import aiohttp
    from bilibili_api import video, Credential
    cookies = site_request_cookies(request, "bilibili.com")
    credential = Credential(sessdata=cookies.get("SESSDATA"), bili_jct=cookies.get("bili_jct"), buvid3=cookies.get("buvid3"), dedeuserid=cookies.get("DedeUserID"))
    url = request['url']
    if urlsplit(url).hostname in ('b23.tv', 'www.b23.tv'):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                url = str(response.url)
    if gallery_reference(url):
        return await download_gallery(url, credential, request, output)
    match = re.search(r'/video/(BV[0-9A-Za-z]+|av\d+)', url)
    if not match:
        raise ValueError('请粘贴 B站单个视频的分享链接')
    identifier = match.group(1)
    page = max(0, int(parse_qs(urlsplit(url).query).get('p', ['1'])[0])-1)
    v = video.Video(bvid=identifier, credential=credential) if identifier.startswith('BV') else video.Video(aid=int(identifier[2:]), credential=credential)
    info = await v.get_info()
    if page >= len(info.get('pages', [])):
        raise ValueError('视频分P不存在')
    original_dimensions = info['pages'][page].get('dimension') or info.get('dimension') or {}
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36', 'Referer': 'https://www.bilibili.com/'}
    tracks = []
    progress = Progress()
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        streams = await select_streams(v, page, original_dimensions, session, headers, request, output, video.VideoDownloadURLDataDetecter)
        for index, source in enumerate(streams):
            if source is None:
                continue
            path = output/('track-'+str(index)+'.m4s.part')
            async with session.get(source.url, headers=headers) as response:
                response.raise_for_status()
                total = response.content_length
                done = 0
                with path.open('xb') as stream:
                    async for chunk in response.content.iter_chunked(256*1024):
                        stream.write(chunk)
                        done += len(chunk)
                        progress(done, total, track=index+1, tracks=len(streams))
                if total and done != total:
                    raise ValueError('音视频传输不完整')
            tracks.append(path)
    emit('progress', phase='postprocess')
    path = output/(identifier+'-p'+str(page+1)+'.mp4')
    partial = path.with_suffix('.part.mp4')
    command = [str(Path(request['ffmpeg'])/'ffmpeg.exe'), '-hide_banner', '-v', 'error', '-n']
    for track in tracks:
        command += ['-i', str(track)]
    if len(tracks) == 2:
        command += ['-map', '0:v:0', '-map', '1:a:0']
    command += ['-c', 'copy', '-movflags', '+faststart', str(partial)]
    merged = subprocess.run(command, capture_output=True, timeout=180,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if merged.returncode:
        raise ValueError('音视频合并失败')
    expected = info['pages'][page].get('duration')
    media = probe(request, partial, expected)
    actual = next(s for s in media['streams'] if s.get('codec_type') == 'video')
    try:
        enforce_quality(original_dimensions, actual, request.get("quality_policy", "original"))
        enforce_selected(actual, request)
    except ValueError:
        partial.unlink(missing_ok=True)
        for track in tracks:
            track.unlink(missing_ok=True)
        raise
    partial.rename(path)
    for track in tracks:
        track.unlink()
    return path, info.get('title', ''), media


def require_original_resolution(original, actual):
    enforce_quality(original, actual)


def run(request):
    output = Path(request['directory']).resolve()
    output.mkdir(parents=True, exist_ok=True)
    emit('progress', phase='inspect')
    if request['command'] != 'download':
        raise ValueError('此平台自动选择可用画质，请直接开始下载')
    platform = request['platform']
    with contextlib.redirect_stdout(Quiet()), contextlib.redirect_stderr(Quiet()):
        if platform == 'douyin':
            path, title, media = douyin(request, output)
        else:
            operation = xiaohongshu if platform == 'xiaohongshu' else bilibili
            path, title, media = asyncio.run(asyncio.wait_for(operation(request, output), 3600))
    if isinstance(path, list):
        complete_assets(request,path,emit,title=title,platform=platform,reencoded=False)
        return
    complete(request, path, emit, title=title, platform=platform, media=media,
             quality_policy=request.get('quality_policy', 'original'), reencoded=False)


if __name__ == '__main__':
    try:
        run(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    except Exception as error:
        fields = dict(code='quality_limited', original=error.original, available=error.available) if isinstance(error, QualityLimitedError) else dict(code='platform')
        emit('failed', error=safe(error), **fields)
        raise SystemExit(1)
