"""Single-job yt-dlp API adapter. Website extraction stays entirely upstream."""
import json
import re
from pathlib import Path
import shutil
import sys
import subprocess
import time
from urllib.parse import urlsplit, parse_qs
try:
    from .download_result import complete
except ImportError:
    from download_result import complete


def safe_error(message):
    return re.sub(r'https?://[^\s\]\)]+', '[链接]', str(message))[-1500:]


def explain_error(error):
    message = safe_error(error)
    lowered = message.lower()
    if 'could not find chrome cookies database' in lowered:
        import os
        root = Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Google/Chrome/User Data'
        for profile in ('Default', 'Profile 1'):
            try:
                (root/profile/'Network/Cookies').stat()
            except PermissionError:
                return 'Windows 拒绝访问 Chrome 登录数据库。请从 Chrome 导出 Netscape 格式 Cookie 文件，在“登录 Cookie”中选择；当前尚未下载成功。'
            except OSError:
                pass
        return '未找到可读取的 Chrome 登录数据库，请确认配置目录，或选择自行导出的 Netscape 格式 Cookie 文件。'
    if any(word in lowered for word in ('fresh cookies', 'sign in', 'login', '登录')):
        return '平台要求有效登录状态，请在“浏览器登录”中主动选择已登录的浏览器，或选择有效 Cookie 文件。\n'+message
    if any(word in lowered for word in ('412', '403', 'forbidden')):
        return '平台拒绝了请求（访问风控），并非下载完成。请使用已登录浏览器的登录状态后重试；登录后仍可能受到平台限制。\n'+message
    if 'unsupported url' in lowered:
        return 'yt-dlp 暂不支持这个页面，或链接被重定向到了非视频页面。请使用单条作品链接。\n'+message
    if any(word in lowered for word in ('decrypt', 'dpapi', 'cookie database')):
        return '无法读取浏览器登录状态，请改用自行导出的 Netscape Cookie 文件。\n'+message
    return message


def emit(event, **fields):
    sys.stdout.write(json.dumps(dict(event=event, **fields), ensure_ascii=False)+'\n')
    sys.stdout.flush()


class Logger:
    def debug(self, message):
        pass
    def warning(self, message):
        emit('warning', message=safe_error(message))
    def error(self, message):
        pass  # The caught DownloadError supplies one terminal failure.


def run(request):
    import yt_dlp
    # Let yt-dlp own every FFmpeg argument; only bound simultaneous merges.
    # Windows abandons the mutex if a cancelled worker is killed.
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        original_run = FFmpegPostProcessor.real_run_ffmpeg
        def bounded_merge(processor, *args, **kwargs):
            handle = kernel.CreateMutexW(None, False, 'Local\\PrismVideoDownloadFFmpeg')
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            acquired = False
            try:
                emit('progress', phase='postprocess', status='waiting')
                acquired = kernel.WaitForSingleObject(handle, 0xffffffff) in (0, 0x80)
                if not acquired:
                    raise OSError('无法取得视频处理资源')
                return original_run(processor, *args, **kwargs)
            finally:
                if acquired:
                    kernel.ReleaseMutex(handle)
                kernel.CloseHandle(handle)
        FFmpegPostProcessor.real_run_ffmpeg = bounded_merge
    output = Path(request['directory']).resolve()
    output.mkdir(parents=True, exist_ok=True)
    private_cookie = Path(request['job_directory'])/'private-cookies.txt'
    last_progress = 0

    def progress(value):
        nonlocal last_progress
        if time.monotonic()-last_progress >= .2 or value.get('status') == 'finished':
            emit('progress', phase='download', downloaded=value.get('downloaded_bytes', 0),
                 total=value.get('total_bytes') or value.get('total_bytes_estimate'),
                 speed=value.get('speed'), eta=value.get('eta'))
            last_progress = time.monotonic()

    def postprocessor(value):
        emit('progress', phase='postprocess', processor=value.get('postprocessor'), status=value.get('status'))

    options = dict(quiet=True, no_warnings=False, logger=Logger(), noplaylist=True,
        retries=2, fragment_retries=2, socket_timeout=20, concurrent_fragment_downloads=2,
        windowsfilenames=True, outtmpl=str(output/'%(extractor)s-%(id)s.%(ext)s'),
        format=request.get('format') or 'bv*+ba/b',
        progress_hooks=[progress], postprocessor_hooks=[postprocessor],
        continuedl=True, overwrites=False)
    if request.get('ffmpeg'):
        options['ffmpeg_location'] = request['ffmpeg']
    if request.get('node'):
        options['js_runtimes'] = {'node': {'path': request['node']}}
    if request.get('cookie_browser'):
        options['cookiesfrombrowser'] = (request['cookie_browser'],)
    if request.get('cookie_file'):
        source = Path(request['cookie_file'])
        if source.stat().st_size > 4*1024*1024:
            raise ValueError('Cookie 文件过大')
        shutil.copyfile(source, private_cookie)
        options['cookiefile'] = str(private_cookie)
    try:
        emit('progress', phase='inspect' if request['command'] == 'inspect' else 'starting')
        with yt_dlp.YoutubeDL(options) as downloader:
            url = request['url']
            if urlsplit(url).hostname in ('xhslink.cn', 'xhslink.com'):
                # A short link can redirect to login?redirectPath=<actual note>.
                # Preserve the shared note URL/token; yt-dlp still extracts it.
                with downloader.urlopen(url) as response:
                    resolved = response.url
                parts = urlsplit(resolved)
                if parts.hostname in ('www.xiaohongshu.com', 'xiaohongshu.com'):
                    redirected = parse_qs(parts.query).get('redirectPath', [None])[0]
                    if parts.path == '/login' and redirected:
                        if redirected.startswith('http://www.xiaohongshu.com/'):
                            redirected = 'https://'+redirected[len('http://'):]
                        if urlsplit(redirected).hostname in ('www.xiaohongshu.com', 'xiaohongshu.com'):
                            url = redirected
                    else:
                        url = resolved
            info = downloader.extract_info(url, download=request['command'] == 'download')
            if not info:
                raise ValueError('网站没有返回可下载的视频信息')
            if info.get('_type') in ('playlist', 'multi_video'):
                raise ValueError('请使用具体单条视频链接，避免自动下载整个合集')
            if request['command'] == 'inspect':
                formats = []
                for item in info.get('formats', [])[-200:]:
                    if item.get('vcodec') == 'none':
                        continue
                    formats.append(dict(id=item['format_id'], extension=item.get('ext'),
                        width=item.get('width'), height=item.get('height'), fps=item.get('fps'),
                        size=item.get('filesize') or item.get('filesize_approx'),
                        audio=item.get('acodec') != 'none'))
                emit('inspected', title=info.get('title', ''), duration=info.get('duration'),
                     platform=info.get('extractor_key', ''), formats=formats)
                return
            candidates = [info.get('filepath'), *[entry.get('filepath') for entry in info.get('requested_downloads', [])],
                          downloader.prepare_filename(info)]
            path = next((Path(value).resolve() for value in candidates if value and Path(value).is_file()), None)
            if path is None:
                raise OSError('下载结束但未找到完整视频文件')
            if not path.is_relative_to(output) or path.name.endswith(('.part', '.ytdl')):
                raise ValueError('下载器返回的文件路径无效')
            probe = subprocess.run([str(Path(request['ffmpeg'])/'ffprobe.exe'),
                '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(path)],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            streams = json.loads(probe.stdout or '{}').get('streams', [])
            if probe.returncode or not any(s.get('codec_type') == 'video' for s in streams):
                raise ValueError('下载文件未通过视频完整性检查，请保留文件后重试')
            complete(request, path, emit, title=info.get('title', ''), platform=info.get('extractor_key', ''))
    finally:
        private_cookie.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        run(request)
    except Exception as error:
        message = explain_error(error)
        lowered = message.lower()
        code = 'auth' if any(word in lowered for word in ('cookie', 'sign in', 'login')) else 'download'
        emit('failed', error=message, code=code)
        raise SystemExit(1)
