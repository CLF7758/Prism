"""The source launcher must not let the console kill the application.

The launcher used to run ``python.exe -m prism`` in its own console window.
python.exe is a console program: it inherits that console, and Windows sends
every attached process the same ``CTRL_CLOSE_EVENT`` when the window is
closed.  Closing the black window therefore terminated Prism as well -- with
no crash report, no faulthandler dump, no exception and no shutdown log,
because the process never got the chance to run any of them.

pythonw.exe is a GUI program: it does not inherit the console at all, so the
window can be closed without touching the application.  A GUI launch has no
console streams either, so the application replaces ``sys.stdout`` and
``sys.stderr`` with the null device.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = sorted(ROOT.glob('*.bat'))
PYTHONW = ROOT / '.venv' / 'Scripts' / 'pythonw.exe'


def test_the_source_launcher_exists():
    assert LAUNCHERS, 'the source launcher (.bat) is missing from the project root'


@pytest.mark.parametrize('launcher', LAUNCHERS, ids=lambda path: path.name)
def test_the_launcher_does_not_run_prism_in_its_own_console(launcher):
    text = launcher.read_text(encoding='utf-8', errors='replace')
    # Every start of the application has to be detached with ``start``: a
    # python.exe that runs in this console inherits it, and closing the window
    # would then kill the application with it.  A pythonw.exe has no console
    # at all; a detached python.exe gets one of its own.
    starts = [line for line in text.splitlines() if '-m prism' in line]
    assert starts, 'the launcher does not start the application at all'
    for line in starts:
        assert 'start ' in line, (
            'the application is started in the launcher console: %s' % line.strip())
    # The application is started with a GUI (console-free) python.
    assert 'pythonw' in text or 'PYEXEW' in text
    # The user has to know that the window may be closed now.
    assert 'closed safely' in text


@pytest.mark.skipif(not PYTHONW.exists(), reason='no pythonw.exe in this checkout')
def test_a_console_free_launch_keeps_the_console_streams_usable():
    """A GUI launch has no stdout/stderr; the application must repair that."""
    environment = dict(os.environ)
    environment.setdefault('QT_QPA_PLATFORM', 'offscreen')
    completed = subprocess.run(
        [str(PYTHONW), '-c',
         'import sys; sys.stdout = None; sys.stderr = None; '
         'import prism.__main__; '
         'sys.exit(0 if (sys.stdout is not None and sys.stderr is not None) else 3)'],
        cwd=str(ROOT), env=environment, timeout=180)
    assert completed.returncode == 0, (
        'a console-free launch left sys.stdout/sys.stderr unusable: '
        'the application would raise on the first print')


def test_the_application_repairs_the_console_streams_at_import_time():
    """The repair belongs to the module, so every launcher gets it."""
    source = (ROOT / 'prism' / '__main__.py').read_text(encoding='utf-8')
    assert 'if sys.stdout is None:' in source
    assert 'if sys.stderr is None:' in source
    # It has to run before the application starts printing anything.
    guard = source.index('if sys.stdout is None:')
    assert guard < source.index('def main():')
