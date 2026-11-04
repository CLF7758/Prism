"""L7: the five thumbnail-read outcomes, and the reply split.

The read function is pure, so all five outcomes can be produced for real -- no
widget, no window, no mock of the function under test.  What is mocked is only
the trace sink, because the point here is "which outcome is recorded".
"""
import io
import json
import os
import types
import sys

import pytest

REPO = r'C:\Users\admin\Desktop\Prism-main\Prism-main'
sys.path.insert(0, REPO)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import QtGui                                            # noqa: E402
import prism.widgets.material_cards as cards                        # noqa: E402


@pytest.fixture()
def records(monkeypatch):
    seen = []
    monkeypatch.setattr(cards, 'trace_record',
                        lambda name, **fields: seen.append((name, fields)))
    return seen


class _Attachments:
    def __init__(self, entries):
        self._entries = entries

    def get(self, key):
        return self._entries.get(key)


def _png(size=4):
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_RGB32)
    image.fill(0xFF3366)
    buffer = __import__('PyQt6.QtCore', fromlist=['QBuffer']).QBuffer()
    buffer.open(__import__('PyQt6.QtCore', fromlist=['QIODevice']).QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


def test_no_thumbnail_record(records, qapp):
    cards.material_thumbnail({'id': 'm1', 'preview': {}}, _Attachments({}))
    outcome = [fields['outcome'] for name, fields in records
               if name == 'material_thumbnail_read']
    assert outcome == ['no_thumbnail_record']


def test_missing_attachment(records, qapp):
    record = {'id': 'm2', 'preview': {'thumbnailAttachmentId': 7}}
    cards.material_thumbnail(record, _Attachments({}))
    outcome = [fields['outcome'] for name, fields in records
               if name == 'material_thumbnail_read']
    assert outcome == ['missing_attachment']
    assert records[-1][1]['attachment'] == 7


def test_decode_failed(records, qapp):
    record = {'id': 'm3', 'preview': {'thumbnailAttachmentId': 8}}
    cards.material_thumbnail(record, _Attachments({8: ('a.png', 'image/png', b'not a png')}))
    outcome = [fields['outcome'] for name, fields in records
               if name == 'material_thumbnail_read']
    assert outcome == ['decode_failed']
    assert records[-1][1]['source_bytes'] == len(b'not a png')


def test_discarded_adjustments(records, qapp):
    record = {'id': 'm4', 'preview': {'discardedAdjustments': True,
                                      'thumbnailAttachmentId': 9}}
    cards.material_thumbnail(record, _Attachments({9: ('a.png', 'image/png', _png())}))
    outcome = [fields['outcome'] for name, fields in records
               if name == 'material_thumbnail_read']
    assert outcome == ['discarded']


def test_ready_is_recorded_with_passthrough(records, qapp):
    payload = _png(64)
    record = {'id': 'm5', 'preview': {'thumbnailAttachmentId': 10}}
    result = cards.material_thumbnail(
        record, _Attachments({10: ('a.png', 'image/png', payload)}))
    assert result and result.startswith('data:image/png;base64,')
    outcome = [fields for name, fields in records
               if name == 'material_thumbnail_read']
    assert outcome and outcome[-1]['outcome'] == 'ready'
    assert outcome[-1]['passthrough'] is True
    assert outcome[-1]['source_bytes'] == len(payload)


def test_reply_splits_pending_and_ready(records, qapp):
    """A reply that does not say how many cards are still pending hides the bug."""
    grid = types.SimpleNamespace()
    grid.assets = [
        {'id': 'a', 'thumbnail': 'data:image/png;base64,AAA', 'thumbnailPending': False},
        {'id': 'b', 'thumbnail': 'data:image/png;base64,BBB', 'thumbnailPending': True},
    ]
    grid._dom_to_id = {'a': 'real-a', 'b': 'real-b'}
    grid.generation = 3
    requested = []
    grid.thumbnail_requested = type('S', (), {'emit': staticmethod(requested.append)})()

    class _Page:
        def runJavaScript(self, script):
            self.script = script

    class _Web:
        def __init__(self):
            self._page = _Page()

        def page(self):
            return self._page

    grid.web = _Web()
    grid._asset_lookup = {asset['id']: asset for asset in grid.assets}
    cards.MaterialCards._send_images(grid, {'a', 'b'})
    reply = [fields for name, fields in records if name == 'material_cards_images']
    assert reply, [name for name, _ in records]
    assert reply[0]['requested'] == 2
    assert reply[0]['pending'] == 1
    assert reply[0]['ready'] == 1
    assert requested == ['real-b'], requested
