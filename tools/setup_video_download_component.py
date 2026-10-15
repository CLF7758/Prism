"""Prepare the isolated upstream yt-dlp/FFmpeg component for source edition."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import venv

PROBE_URL = 'https://registry.npmjs.org/@ffprobe-installer/win32-x64/-/win32-x64-5.1.0.tgz'
PROBE_SHA512 = 'jMGYeAgkrdn4e2vvYt/qakgHRE3CPju4bn5TmdPfoAm1BlX1mY9cyMd8gf5vSzI8gH8Zq5WQAyAkmekX/8TSTg=='


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-url', default='https://pypi.org/simple')
    parser.add_argument('--skip-dependencies', action='store_true')
    args = parser.parse_args()
    root = Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Prism/components/video-download'
    root.mkdir(parents=True, exist_ok=True)
    python = root/'runtime/Scripts/python.exe'
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(root/'runtime')
    if not args.skip_dependencies:
        subprocess.run([str(python), '-m', 'pip', 'install', '--index-url', args.index_url,
            'yt-dlp[default]==2026.8.19', 'imageio-ffmpeg==0.6.0'], check=True)
    binaries = root/'bin'
    binaries.mkdir(exist_ok=True)
    ffmpeg = subprocess.check_output([str(python), '-c',
        'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], text=True).strip()
    shutil.copyfile(ffmpeg, binaries/'ffmpeg.exe')
    if not (binaries/'ffprobe.exe').exists():
        with urllib.request.urlopen(PROBE_URL, timeout=60) as response:
            archive = response.read(40*1024*1024)
        if base64.b64encode(hashlib.sha512(archive).digest()).decode() != PROBE_SHA512:
            raise ValueError('ffprobe archive integrity mismatch')
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
            with package.extractfile('package/ffprobe.exe') as source, (binaries/'ffprobe.exe').open('wb') as output:
                shutil.copyfileobj(source, output)
            for member in package.getmembers():
                if member.isfile() and ('license' in member.name.lower() or 'readme' in member.name.lower()):
                    (binaries/Path(member.name).name).write_bytes(package.extractfile(member).read())
    freeze = subprocess.check_output([str(python), '-m', 'pip', 'freeze'], text=True)
    (root/'requirements-installed.txt').write_text(freeze, encoding='utf-8')
    (root/'component.json').write_text(json.dumps(dict(
        upstream='https://github.com/yt-dlp/yt-dlp', version='2026.8.19',
        ffmpeg='https://github.com/imageio/imageio-ffmpeg', ffprobe=PROBE_URL,
        ffprobe_integrity='sha512-'+PROBE_SHA512,
        node=shutil.which('node'), protocol_version=1), indent=2), encoding='utf-8')
    print('Prepared video component:', root)
    if not shutil.which('node'):
        print('Node.js 22+ recommended for YouTube JavaScript challenges.')


if __name__ == '__main__':
    main()
