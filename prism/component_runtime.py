"""Launch a venv's packages directly, without the Windows venv redirector."""
from pathlib import Path


def invocation(runtime, script, arguments=()):
    runtime = Path(runtime)
    settings = dict(line.split(' = ', 1) for line in (runtime/'pyvenv.cfg').read_text(encoding='utf-8').splitlines() if ' = ' in line)
    python = runtime.parent.parent/'python-base'/'python.exe'
    if not python.is_file():
        python = Path(settings['home'])/'python.exe'
    if not python.is_file():
        raise OSError('组件 Python 运行环境不可用')
    bootstrap = Path(__file__).parent/'components'/'process_bootstrap.py'
    return str(python), ['-I', '-S', str(bootstrap), str(runtime), str(script), *map(str, arguments)]
