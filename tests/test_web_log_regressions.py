"""Bundled WebEngine pages in an isolated, strictly offscreen process."""
import os
from pathlib import Path
import subprocess
import sys


def test_hidden_mindmap_clipboard_and_legacy_preview_config():
    script = r'''
import json, time
from types import SimpleNamespace
from pathlib import Path
from PyQt6 import QtCore
QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6 import QtWidgets, QtWebEngineWidgets, QtWebEngineCore
app = QtWidgets.QApplication(['prism-offscreen-regression'])
assert app.platformName() == 'offscreen'
from prism.widgets.mindmap_panel import MindMapPanel

def wait(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('Timed out waiting for page result')

def js(page, expression):
    result = []
    page.runJavaScript(expression, result.append)
    wait(lambda: bool(result))
    return result[0]

scene = SimpleNamespace(project_id='offscreen', mindmap_tree=None)
panel = MindMapPanel(SimpleNamespace(scene=scene))
panel.resize(1000, 700)
results = []
panel._bridge.treeLoaded.connect(lambda ok, error: results.append((ok, error)))
panel.show()
wait(lambda: panel._ready)
wait(lambda: bool(results))
assert results[-1][0], results
page = panel.web_view.page()
js(page, "document.getElementById('map').style.display='none'")
old = {'data': {'text': 'old-hidden'}, 'children': []}
new = {'data': {'text': 'latest-hidden'}, 'children': [{'data': {'text': 'copy-child'}, 'children': []}]}
results.clear()
panel._bridge.loadTree.emit(json.dumps({'tree': old}))
panel._bridge.loadTree.emit(json.dumps({'tree': new}))
js(page, 'true')  # Drain both bridge messages without changing the viewport.
assert not results, results
js(page, "document.getElementById('map').style.display=''")
wait(lambda: bool(results))
assert results == [(True, '')], results
assert js(page, "(()=>{const e=document.createElement('div');e.innerHTML=__mindMap.getData().data.text;return e.textContent;})()") == 'latest-hidden'

# This platform's clipboard is private memory, not the user's Windows clipboard.
app.clipboard().setText('before-copy')
js(page, "__mindMap.renderer.activeNodeList=[__mindMap.renderer.root.children[0]];__mindMap.renderer.copy();true")
wait(lambda: app.clipboard().text() != 'before-copy')
copied = json.loads(app.clipboard().text())
assert copied['simpleMindMap'] and 'copy-child' in copied['data'][0]['data']['text'], copied
js(page, '__mindMap.renderer.activeNodeList=[__mindMap.renderer.root];__mindMap.renderer.paste();true')
wait(lambda: js(page, '__mindMap.getData().children.length') == 2)
app.clipboard().setText('external pasted text')
js(page, '__mindMap.renderer.activeNodeList=[__mindMap.renderer.root];__mindMap.renderer.paste();true')
wait(lambda: js(page, '__mindMap.getData().children.length') == 3)
assert 'external pasted text' in js(page, 'JSON.stringify(__mindMap.getData())')

# Load the real renderer page, but exercise configuration before any GPU work.
render_page = QtWebEngineCore.QWebEnginePage()
render_page.setUrl(QtCore.QUrl.fromLocalFile(str(Path('prism/assets/materials/render.html').resolve())))
wait(lambda: js(render_page, 'typeof previewProfile') == 'function')
js(render_page, "previewProfile().then(p=>window.profileProbe=p).catch(e=>window.profileFailure=String(e));true")
wait(lambda: js(render_page, '!!(window.profileProbe||window.profileFailure)'))
assert not js(render_page, 'window.profileFailure||null')
profile = js(render_page, 'window.profileProbe')
assert profile == json.loads(Path('prism/assets/materials/preview-profile.json').read_text(encoding='utf-8'))
panel._payload_cancel.set()
panel._page.deleteLater()
render_page.deleteLater()
panel.deleteLater()
QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
app.processEvents()
print('hidden-latest-tree, native clipboard copy/paste, local shared profile: passed')
'''
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8',
                       QTWEBENGINE_CHROMIUM_FLAGS='--disable-gpu --disable-software-rasterizer --no-sandbox')
    result = subprocess.run([sys.executable, '-c', script], env=environment,
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, encoding='utf-8',
                            timeout=80, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout + result.stderr
