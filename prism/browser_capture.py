"""Loopback-only HTTP bridge used by the Prism browser extension."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PyQt6 import QtCore


logger = logging.getLogger(__name__)


class BrowserCaptureService(QtCore.QObject):
    """Receive browser image URLs without touching Qt from a server thread."""

    capture_received = QtCore.pyqtSignal(str, str, list)
    status_changed = QtCore.pyqtSignal(str)

    def __init__(self, port=47653, parent=None):
        super().__init__(parent)
        self.port = int(port)
        self._server = None
        self._thread = None

    def start(self):
        if self._server is not None:
            return
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'PrismCapture/1.0'

            def _headers(self, status=200, content_type='application/json'):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-store')
                origin = self.headers.get('Origin', '')
                if origin.startswith('chrome-extension://'):
                    self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Prism-Capture')
                self.end_headers()

            def do_OPTIONS(self):
                self._headers(204)

            def do_GET(self):
                if self.path != '/health':
                    self._headers(404)
                    return
                self._headers()
                self.wfile.write(json.dumps({
                    'app': 'Prism', 'capture': True,
                    'port': service.port,
                }).encode('utf-8'))

            def do_POST(self):
                origin = self.headers.get('Origin', '')
                if (self.headers.get('X-Prism-Capture') != '1'
                        or (origin and not origin.startswith('chrome-extension://'))):
                    self._headers(403)
                    return
                if self.path != '/capture':
                    self._headers(404)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    size = 0
                if size <= 0 or size > 1024 * 1024:
                    self._headers(413)
                    return
                try:
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError('Expected a JSON object')
                    url = str(payload.get('url', '')).strip()
                    parsed = urlparse(url)
                    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                        raise ValueError('Only http(s) image URLs are accepted')
                    title = str(payload.get('title', ''))[:500]
                    if not isinstance(payload.get('tags', []), list):
                        raise ValueError('Expected a tag list')
                    tags = [str(tag).strip()[:100]
                            for tag in payload.get('tags', [])[:50]
                            if str(tag).strip()]
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._headers(400)
                    self.wfile.write(json.dumps(
                        {'ok': False, 'error': str(exc)}).encode('utf-8'))
                    return
                service.capture_received.emit(url, title, tags)
                self._headers(202)
                self.wfile.write(b'{"ok": true}')

            def log_message(self, fmt, *args):
                logger.debug('Browser capture: ' + fmt, *args)

        try:
            self._server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        except OSError as exc:
            logger.warning('Could not start browser capture service: %s', exc)
            self.status_changed.emit(str(exc))
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name='PrismBrowserCapture', daemon=True)
        self._thread.start()
        logger.info('Browser capture listening on http://127.0.0.1:%s', self.port)
        self.status_changed.emit('running')

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
