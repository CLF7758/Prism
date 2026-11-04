"""Run the real worker entry point in a fresh process and report what it did.

The worker must not end its process when the user closes the preview window:
the helper costs about 2.7 s to start, so a preview that ends it makes the
next preview pay that again.

This script runs ``prism.components.material_preview.main()`` with a fake
channel, closes the preview window while the worker is serving, and prints
one JSON line with what happened.  It runs in its own process because the
worker owns a VTK render window, and a native object that outlives the
application tears the interpreter down.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r'''
import json, sys
from PyQt6 import QtCore, QtWidgets
from prism.model_library import native_protocol as wire
from prism.widgets.model_preview import ModelPreviewWindow

instances = []
class FakeChannel:
    def __init__(self):
        self.sent = []
        self.closed = False
        instances.append(self)
    @classmethod
    def connect(cls, host, port, timeout=None):
        return cls()
    def send(self, message):
        self.sent.append(message)
    def recv(self, timeout=None):
        return None
    def close(self):
        self.closed = True

wire.Channel = FakeChannel
sys.argv = ['material_preview', '--ipc-port', '1', '--ipc-token', 'token',
            '--cache-root', sys.argv[1]]

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
seen = {}

def close_the_window():
    shells = [widget for widget in app.topLevelWidgets()
              if isinstance(widget, ModelPreviewWindow)]
    if not shells:
        seen['no_shell'] = True
        return
    shells[0].close()
    seen['closed'] = True

def quit_later():
    # The only thing that may end the event loop.
    seen['quit'] = True
    app.quit()

QtCore.QTimer.singleShot(300, close_the_window)
QtCore.QTimer.singleShot(900, quit_later)

from prism.components import material_preview
material_preview.main()

channel = instances[0]
events = [message.get('event') for message in channel.sent
          if message.get('type') == wire.EVENT]
report = dict(closed=bool(seen.get('closed')),
              quit_by_timer=bool(seen.get('quit')),
              no_shell=bool(seen.get('no_shell')),
              events=events,
              bye=any(message.get('type') == wire.BYE for message in channel.sent),
              channel_closed=bool(channel.closed))
print('REPORT ' + json.dumps(report), flush=True)
for widget in app.topLevelWidgets():
    widget.deleteLater()
app.processEvents()
'''


def test_the_worker_keeps_running_after_the_window_closed(tmp_path):
    """The worker must not end its process when the user closes the window.

    Before the fix it quit on the close, sent ``BYE`` and the helper was
    gone: the next preview had to start a new one, which costs about 2.7 s.
    """
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([sys.executable, '-u', '-c', SCRIPT, str(tmp_path)],
                            cwd=str(ROOT), env=environment,
                            capture_output=True, text=True, timeout=120)
    report = {}
    for line in result.stdout.splitlines():
        if line.startswith('REPORT '):
            report = json.loads(line[len('REPORT '):])
    assert report, 'the worker did not report: %s %s' % (result.stdout[-500:],
                                                         result.stderr[-500:])
    assert report['closed'], 'the preview window was never closed'
    assert report['quit_by_timer'], 'the worker ended its own process on the close'
    assert 'material.preview_closed' in report['events'], \
        'the desktop was not told that the preview closed: %r' % (report['events'],)
    assert not report['bye'], \
        'the worker said goodbye: the helper would have to be started again'
    assert not report['channel_closed'], 'the channel was closed'
