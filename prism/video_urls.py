"""Normalize copied share text; actual site extraction belongs to yt-dlp."""
import html
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def normalize_share_url(text):
    text = html.unescape(text).replace('https\\:', 'https:').replace('http\\:', 'http:').replace('\\&', '&')
    matches = re.findall(r'https?://[^\s<>\[\]）]+', text)
    if not matches:
        raise ValueError('未找到视频链接，可以直接粘贴平台分享文案')
    url = matches[0].rstrip(').,，。;；')
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError('视频链接无效')
    # Tracking parameters are irrelevant to extraction and need not be stored.
    if parts.hostname.endswith('bilibili.com'):
        query = [(key, value) for key, value in parse_qsl(parts.query) if key in ('p', 't')]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    return url
