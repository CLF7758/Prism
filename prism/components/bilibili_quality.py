"""Quality negotiation around the upstream Bilibili stream detector."""
import json
from pathlib import Path
import subprocess


class QualityLimitedError(ValueError):
    def __init__(self, original, actual):
        self.original = {k:int(original[k]) for k in ('width','height')}
        self.available = {k:int(actual[k]) for k in ('width','height')}
        super().__init__(f"原视频 {self.original['width']}×{self.original['height']}，当前会话最高可用 {self.available['width']}×{self.available['height']}。可提供有效 B站会话继续获取原分辨率，或明确选择下载当前画质；不会自动降清晰度。")


def resolution(value):
    return sorted((int(value.get('width') or 0),int(value.get('height') or 0)))


def enforce_quality(original, actual, policy='original'):
    if policy not in ('original','best_available'):
        raise ValueError('未知的视频画质选择')
    expected,got=resolution(original),resolution(actual)
    if policy == 'original' and expected[0] and got[0] and any(a<b for a,b in zip(got,expected)):
        raise QualityLimitedError(original,actual)


async def probe_progressive(session, url, headers, request, output):
    """Read a bounded MP4 header; never trust API quality labels as dimensions."""
    path=Path(output)/'quality-probe.mp4.part'
    limit=8*1024*1024
    try:
        async with session.get(url,headers=headers | {'Range':f'bytes=0-{limit-1}'}) as response:
            response.raise_for_status()
            with path.open('xb') as stream:
                done=0
                async for chunk in response.content.iter_chunked(65536):
                    chunk=chunk[:limit-done]
                    stream.write(chunk);done+=len(chunk)
                    if done>=limit:break
        result=subprocess.run([str(Path(request['ffmpeg'])/'ffprobe.exe'),'-v','quiet',
            '-analyzeduration','0','-show_entries','stream=codec_type,width,height',
            '-of','json',str(path)],capture_output=True,timeout=20,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            return {}
        return next((v for v in json.loads(result.stdout).get('streams',[]) if v.get('codec_type')=='video'),{})
    finally:
        path.unlink(missing_ok=True)


def enforce_selected(actual, request):
    minimum=request.get('minimum_dimensions') or {}
    expected,got=resolution(minimum),resolution(actual)
    if expected[0] and got[0] and any(a<b for a,b in zip(got,expected)):
        raise ValueError('当前可用画质低于刚才选择的分辨率，尚未导入；请重新获取画质后确认。')


async def select_streams(v, page, original, session, headers, request, output, detector):
    data=await v.get_download_url(page_index=page)
    streams=detector(data).detect_best_streams()
    if not streams or not streams[0]:
        raise ValueError('没有可用的视频流')
    actual=next((s for s in data.get('dash',{}).get('video',[])
        if (s.get('baseUrl') or s.get('base_url'))==streams[0].url),{})
    # Try the upstream's documented HTML5 playback mode when DASH is limited.
    # It may expose a better public progressive rendition (verified by ffprobe).
    if resolution(actual)<resolution(original) or resolution(actual)[0]==0:
        html5=await v.get_download_url(page_index=page,html5=True)
        if html5.get('durl'):
            alternatives=detector(html5).detect_best_streams()
            if alternatives and alternatives[0]:
                candidate=await probe_progressive(session,alternatives[0].url,headers,request,output)
                if resolution(candidate)>resolution(actual):
                    streams,actual=alternatives,candidate
    enforce_quality(original,actual,request.get('quality_policy','original'))
    enforce_selected(actual,request)
    return streams
