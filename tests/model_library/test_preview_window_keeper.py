"""Closing the preview window must not end the helper.

The preview lives in its own helper process, and that process costs about
2.7 s to start: it loads VTK and creates an OpenGL context.  The controller
keeps one helper for the whole session, and the desktop says so itself when
the window closes ("the next edit reloads in the worker").  So the window is
hidden instead of closed, and the model is unloaded to give its memory back.

These tests pin the two halves of that:

* the window survives a close and can be shown again,
* unloading drops the model but keeps the window and its context.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_a_closed_preview_window_is_hidden_and_survives(qapp, qtbot):
    from PyQt6 import sip
    from prism.widgets.model_preview import ModelPreviewWindow, PreviewWindowKeeper
    shell = ModelPreviewWindow()
    qtbot.addWidget(shell)
    closed = []
    keeper = PreviewWindowKeeper(shell, lambda: closed.append(True))
    shell.show()
    shell.close()
    assert closed == [True], 'the host was not told that the window closed'
    assert not shell.isVisible(), 'the closed window is still on screen'
    assert not sip.isdeleted(shell), 'the window was deleted instead of hidden'
    # The next preview shows the same window again.
    shell.showNormal()
    assert shell.isVisible(), 'the window could not be shown again'
    assert keeper.shell is shell


def test_without_the_keeper_the_closed_window_is_deleted(qapp):
    """The A/B half: the shell deletes itself on close.

    This is why the worker needs the keeper: ``WA_DeleteOnClose`` takes the
    native widget inside the shell with it.
    """
    from PyQt6 import sip
    from prism.widgets.model_preview import ModelPreviewWindow
    shell = ModelPreviewWindow()
    shell.show()
    shell.close()
    qapp.processEvents()
    assert sip.isdeleted(shell), 'the shell no longer deletes itself on close'


def test_unloading_drops_the_model_and_keeps_the_window(tmp_path):
    script = r'''
from pathlib import Path
import sys
from prism.model_library.multimaterial.native.renderer import MaterialRenderer
root=Path(sys.argv[1])
part=dict(binding_id='p',points=[[-1,-1,0],[1,-1,0],[0,1,0]],faces=[[0,1,2]],
          normals=[[0,0,1]]*3,matrix=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],uvs={})
materials={'m':dict(color=[1,0,0],unlit=True)}
assignments={'p':'m'}
r=MaterialRenderer()
try:
    r.load([part],materials,assignments)
    assert r.actors, 'the model was not loaded'
    props=r.renderer.GetViewProps()
    assert props.GetNumberOfItems()>0, 'the model produced no view props'
    r.unload()
    assert not r.actors, 'the actors survived the unload'
    assert r.renderer.GetViewProps().GetNumberOfItems()==0, 'the view props survived the unload'
    assert not r._images and not r._active_images, 'the textures survived the unload'
    # The window is still alive: the same renderer reads the model again.
    r.load([part],materials,assignments)
    assert r.actors, 'the renderer could not read the model again'
    r.png(root/'again.png')
    assert (root/'again.png').stat().st_size>0
finally:
    r.close()
'''
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            cwd=str(ROOT), env=environment,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
