"""Validate image originals and generate tiny previews using bundled FFmpeg."""
import json
from pathlib import Path
import subprocess


def image_asset(request, path, expected=None):
    path=Path(path)
    binary=Path(request['ffmpeg'])
    flags={'capture_output':True,'timeout':30,'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)}
    checked=subprocess.run([str(binary/'ffprobe.exe'),'-v','quiet','-show_entries',
        'stream=codec_name,width,height','-of','json',str(path)],**flags)
    streams=json.loads(checked.stdout or '{}').get('streams',[])
    if checked.returncode or not streams:
        raise ValueError('下载内容不是可解码图片')
    stream=streams[0]
    suffix={'mjpeg':'.jpg','png':'.png','webp':'.webp','av1':'.avif','hevc':'.heic','gif':'.gif','bmp':'.bmp','tiff':'.tiff'}.get(stream.get('codec_name'))
    if not suffix or min(stream.get('width',0),stream.get('height',0))<=0:
        raise ValueError('下载内容缺少有效图片尺寸')
    if expected and all(expected.get(k) for k in ('width','height')) and any(stream[k]<int(expected[k]) for k in ('width','height')):
        raise ValueError('平台返回的图片小于作品声明的原图尺寸，未自动导入低清替代')
    preview=path.with_name(path.stem+'-micro.png')
    decoded=subprocess.run([str(binary/'ffmpeg.exe'),'-hide_banner','-v','error','-xerror','-n',
        '-i',str(path),'-frames:v','1','-vf','scale=8:8:force_original_aspect_ratio=decrease',str(preview)],**flags)
    if decoded.returncode or not preview.is_file():
        raise ValueError('下载图片未通过解码检查')
    target=path.with_suffix(suffix)
    if path!=target:path.rename(target)
    return dict(asset_type='image',path=str(target),preview_path=str(preview),
        image=dict(width=stream['width'],height=stream['height'],format='jpeg' if suffix=='.jpg' else suffix[1:]))


def stream_file(session, urls, path, headers, progress):
    if not urls:
        raise ValueError('作品没有可用的素材地址')
    path=Path(path)
    response=session.get(urls[0],headers=headers,stream=True,timeout=120)
    try:
        response.raise_for_status()
        total=int(response.headers.get('Content-Length','0')) or None
        done=0
        with path.open('xb') as output:
            for chunk in response.iter_content(256*1024):
                if chunk:output.write(chunk);done+=len(chunk);progress(done,total)
        if done==0 or (total and done!=total):
            raise ValueError('素材下载不完整')
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:response.close()
    return path
