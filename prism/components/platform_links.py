"""First-party share-link normalization, without handling site extraction."""
import re
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit, unquote


def xhs_destination(url):
    parts = urlsplit(url)
    if parts.hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
        raise ValueError('小红书短链接没有指向作品页面')
    if parts.path.startswith('/website-login/captcha'):
        raise ValueError('小红书对下载器返回了验证页面，本次已停止请求；这不代表原链接失效。')
    query = parse_qs(parts.query)
    nested = query.get('redirectPath', [''])[0]
    if parts.path == '/login' and nested:
        return xhs_destination(urljoin('https://www.xiaohongshu.com/', nested))
    message = unquote(query.get('undertake_note_error', [''])[0])[:200]
    identifier = query.get('target_note_id', [''])[0]
    path = parts.path
    if path.rstrip('/') == '/explore' and re.fullmatch(r'[0-9a-fA-F]{24}', identifier):
        path = '/explore/'+identifier
    # Do not carry error-route flags or personal tracking fields into the
    # canonical note request. Preserve only the shared public access token.
    kept = [(key, value[0]) for key, value in query.items() if key in ('xsec_token', 'xsec_source')]
    return urlunsplit(('https', 'www.xiaohongshu.com', path, urlencode(kept), '')), message


def site_request_cookies(request, site):
    """Accept only the explicitly supplied site's unexpired Netscape cookies."""
    filename = request.get('cookie_file')
    if not filename:
        return {}
    path = Path(filename)
    if path.stat().st_size > 4*1024*1024:
        raise ValueError('Cookie 文件过大')
    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (ValueError, OSError) as error:
        raise ValueError('无法读取所选 Netscape Cookie 文件') from error
    cookies = [c for c in jar if (c.domain.lstrip('.').lower() == site
               or c.domain.lower().endswith('.'+site))
               and (c.expires in (None, 0) or c.expires > time.time())]
    if not cookies:
        raise ValueError(f'所选文件没有未过期的 {site} 会话')
    if any('\r' in c.name+c.value or '\n' in c.name+c.value for c in cookies):
        raise ValueError('Cookie 文件格式无效')
    return {c.name:c.value for c in cookies}


def xhs_request_cookie(request):
    return '; '.join(k+'='+v for k,v in site_request_cookies(request,'xiaohongshu.com').items())


def xhs_page_failure(state):
    """Describe upstream's empty note result without logging response secrets."""
    if not isinstance(state, dict):
        return ''
    note = state.get('note')
    info = note.get('serverRequestInfo') if isinstance(note, dict) else None
    if not isinstance(info, dict) or info.get('state') != 'fail':
        return ''
    code = info.get('errorCode')
    code = str(code) if isinstance(code, int) and not isinstance(code, bool) else '未知'
    return f'小红书未向下载器返回笔记正文（平台状态 {code}），本次尚未下载成功。不能据此判断原链接失效。'


def douyin_identifier(url, session, fetcher):
    def extract(candidate):
        host = urlsplit(candidate).hostname or ''
        if not any(host == domain or host.endswith('.'+domain) for domain in ('douyin.com', 'iesdouyin.com')):
            return None
        for pattern in (fetcher._DOUYIN_VIDEO_URL_PATTERN, fetcher._DOUYIN_NOTE_URL_PATTERN, fetcher._DOUYIN_INVAILID_URL_PATTERN):
            match = pattern.search(candidate)
            if match and re.fullmatch(r'\d{15,22}', match.group(1)):
                return match.group(1)
    identifier = extract(url)
    if identifier:
        return identifier
    # F2's URL patterns are reused, with the same browser-compatible
    # transport used by its detail API. Avoid the separate httpx 503 path.
    response = session.get(url, timeout=30, max_redirects=5)
    identifier = extract(str(response.url))
    if identifier:
        return identifier
    response.raise_for_status()
    raise ValueError('无法解析抖音视频短链接，请检查链接是否仍可访问')
