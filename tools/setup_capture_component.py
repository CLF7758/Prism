"""Install Prism's isolated, pinned ScreenPinKit component (source edition)."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

COMMIT = '1c0dc5fa14a9ec99660fbb3bc6eade7d7d1c3f77'
HOTKEY_COMMIT = 'e3e9d308e7dd633c8ecbb9515de5e93b1975f01d'
DEPENDENCIES = ['PyQt5==5.15.11', 'PyQtWebEngine==5.15.7',
    'PyQt-Fluent-Widgets==1.11.3', 'numpy==1.26.4', 'scipy==1.16.3',
    'opencv-contrib-python==4.10.0.84', 'onnxruntime==1.19.2',
    'shapely==2.0.6', 'pyclipper', 'fuzzywuzzy', 'python-Levenshtein',
    'sortedcontainers', 'requests', 'pillow']


def run(*command):
    subprocess.run(list(map(str, command)), check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-url', default='https://pypi.org/simple')
    parser.add_argument('--skip-dependencies', action='store_true')
    args = parser.parse_args()
    directory = Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Prism/components/capture'
    directory.mkdir(parents=True, exist_ok=True)
    runtime = directory/'runtime'
    python = runtime/'Scripts/python.exe'
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(runtime)
    upstream = directory/'upstream-source'
    if not upstream.exists():
        run('git', '-c', 'http.version=HTTP/1.1', 'clone', '--depth', '1',
            'https://github.com/InterwovenCode/ScreenPinKit.git', upstream)
    actual = subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != COMMIT:
        raise RuntimeError('Upstream revision differs from the reviewed commit; refusing to overwrite it')
    if not args.skip_dependencies:
        run(python, '-m', 'pip', 'install', '--index-url', args.index_url, '--timeout', '30', *DEPENDENCIES)
        hotkey = directory/'system-hotkey-source'
        if not hotkey.exists():
            run('git', '-c', 'http.version=HTTP/1.1', 'clone', '--depth', '1',
                'https://github.com/YaoXuanZhi/system_hotkey.git', hotkey)
        revision = subprocess.check_output(['git', '-C', str(hotkey), 'rev-parse', 'HEAD'], text=True).strip()
        if revision != HOTKEY_COMMIT:
            raise RuntimeError('Hotkey dependency differs from the reviewed revision')
        run(python, '-m', 'pip', 'install', '--index-url', args.index_url, '--no-deps', hotkey)
    # Isolate upstream preferences from any standalone ScreenPinKit install.
    config = upstream/'src/common/config.py'
    text = config.read_text(encoding='utf-8')
    old = 'configFolder = os.path.join(os.path.expanduser("~"), f".{APP_NAME}")'
    new = 'configFolder = os.environ.get("PRISM_CAPTURE_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), f".{APP_NAME}")'
    if old not in text and new not in text:
        raise RuntimeError('Upstream configuration hook changed')
    config.write_text(text.replace(old, new), encoding='utf-8')
    freeze = subprocess.check_output([str(python), '-m', 'pip', 'freeze'], text=True)
    (directory/'requirements-installed.txt').write_text(freeze, encoding='utf-8')
    manifest = dict(repository='https://github.com/InterwovenCode/ScreenPinKit',
        commit=COMMIT, source=str(upstream/'src'), license='MIT',
        adapter_patch='common.config: optional PRISM_CAPTURE_CONFIG_HOME',
        hotkey_repository='https://github.com/YaoXuanZhi/system_hotkey',
        hotkey_commit=HOTKEY_COMMIT, protocol_version=1)
    (directory/'upstream.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('ScreenPinKit component prepared:', directory)


if __name__ == '__main__':
    main()
