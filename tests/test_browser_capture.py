import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from prism.browser_capture import BrowserCaptureService


def test_capture_rejects_websites_and_accepts_extension(qtbot):
    service = BrowserCaptureService(port=0)
    service.start()
    address = f'http://127.0.0.1:{service._server.server_port}/capture'
    payload = json.dumps({'url': 'https://example.com/image.png',
                          'title': 'Reference', 'tags': ['night']}).encode()
    try:
        blocked = Request(address, data=payload, headers={
            'Origin': 'https://untrusted.example', 'Content-Type': 'application/json',
            'X-Prism-Capture': '1'})
        with pytest.raises(HTTPError) as error:
            urlopen(blocked, timeout=2)
        assert error.value.code == 403
        allowed = Request(address, data=payload, headers={
            'Origin': 'chrome-extension://test-extension',
            'Content-Type': 'application/json', 'X-Prism-Capture': '1'})
        with qtbot.waitSignal(service.capture_received) as signal:
            with urlopen(allowed, timeout=2) as response:
                assert response.status == 202
        assert signal.args == ['https://example.com/image.png', 'Reference', ['night']]
    finally:
        service.stop()
