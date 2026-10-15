"""Explicit, isolated website login using QtWebEngine's own session APIs."""
from pathlib import Path
from urllib.parse import urlsplit
from PyQt6 import QtCore, QtWidgets

PLATFORMS = {
    'douyin': ('douyin.com', 'iesdouyin.com'),
    'xiaohongshu': ('xiaohongshu.com', 'xhslink.cn', 'xhslink.com'),
    'bilibili': ('bilibili.com', 'b23.tv'),
}


def platform_for_url(url):
    host = (urlsplit(url).hostname or '').lower()
    return next((name for name, domains in PLATFORMS.items()
                 if any(host == domain or host.endswith('.'+domain) for domain in domains)), None)


def session_file(cookies, platform, destination):
    """Export only this site's cookies, never another browser's database."""
    rows = ['# Netscape HTTP Cookie File']
    for cookie in cookies:
        domain = cookie.domain()
        if not any(domain.lstrip('.') == allowed or domain.lstrip('.').endswith('.'+allowed)
                   for allowed in PLATFORMS[platform]):
            continue
        name = bytes(cookie.name()).decode('utf-8')
        value = bytes(cookie.value()).decode('utf-8')
        path = cookie.path() or '/'
        if any(character in field for field in (domain, name, value, path) for character in ('\t', '\r', '\n')):
            continue
        expires = 0 if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch()
        if expires and expires <= QtCore.QDateTime.currentSecsSinceEpoch():
            continue
        rows.append('\t'.join([('#HttpOnly_' if cookie.isHttpOnly() else '')+domain,
            'TRUE' if domain.startswith('.') else 'FALSE', path,
            'TRUE' if cookie.isSecure() else 'FALSE', str(expires), name, value]))
    if len(rows) == 1:
        raise ValueError('当前还没有可用的网站会话，请先完成页面中的登录或验证')
    destination = Path(destination)
    temporary = destination.with_suffix('.writing')
    temporary.write_text('\n'.join(rows)+'\n', encoding='utf-8')
    temporary.replace(destination)
    return str(destination)


class WebsiteLogin(QtWidgets.QDialog):
    session_ready = QtCore.pyqtSignal(str, str, str)

    def __init__(self, url, directory, parent=None):
        super().__init__(parent)
        from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        self.platform = platform_for_url(url)
        if self.platform is None:
            raise ValueError('这个入口用于抖音、小红书和 B站的登录')
        self.directory = Path(directory)
        self.cookies = {}
        self.setWindowTitle('网站登录 · Prism 独立会话')
        self.resize(1000, 760)
        layout = QtWidgets.QVBoxLayout(self)
        self.status = QtWidgets.QLabel('请在下方完成网站登录或验证，再点击“使用此会话”。这不会读取 Chrome 登录数据库。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        # Off-the-record profile: session is destroyed with the dialog; only a
        # site-filtered temporary Netscape snapshot is passed to yt-dlp.
        self.profile = QWebEngineProfile(self)
        self.page = QWebEnginePage(self.profile, self)
        self.browser = QWebEngineView(self)
        self.browser.setPage(self.page)
        self.profile.cookieStore().cookieAdded.connect(self.add_cookie)
        self.profile.cookieStore().cookieRemoved.connect(self.remove_cookie)
        layout.addWidget(self.browser, 1)
        self.use = QtWidgets.QPushButton('使用此会话返回下载窗口')
        self.use.clicked.connect(self.finish_login)
        layout.addWidget(self.use)
        self.browser.setUrl(QtCore.QUrl(url))

    def add_cookie(self, cookie):
        if len(self.cookies) < 512:
            self.cookies[(cookie.domain(), cookie.path(), bytes(cookie.name()))] = cookie

    def remove_cookie(self, cookie):
        self.cookies.pop((cookie.domain(), cookie.path(), bytes(cookie.name())), None)

    def finish_login(self):
        try:
            path = session_file(self.cookies.values(), self.platform, self.directory/(self.platform+'.cookies.txt'))
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.session_ready.emit(path, self.profile.httpUserAgent(), self.platform)
        self.accept()

    def closeEvent(self, event):
        self.browser.stop()
        self.page.setWebChannel(None)
        event.accept()
