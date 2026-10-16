"""Prepare isolated, pinned open-source video backends for source/installer.

--from-verified reuses the already verified local tool installations. The default
downloads the same pinned upstream sources and installs their requirements.
Only library/source files are staged: never user cookies, downloads or settings.
"""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import venv
import zipfile

F2 = 'f6be8c0ffba9a127075bbeafe4838716650b6325'
XHS = '3261312721f0b37c705ba6515885bc7f34349f2f'
XHSHOW = 'b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1'


def source_archive(repo, commit, target):
    with urllib.request.urlopen(f'https://codeload.github.com/{repo}/zip/{commit}', timeout=90) as response:
        data = response.read(50*1024*1024+1)
    if len(data) > 50*1024*1024:
        raise ValueError('Upstream archive exceeds limit')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        prefix = archive.namelist()[0].split('/')[0]+'/'
        for name in archive.namelist():
            relative = Path(name.removeprefix(prefix))
            dest = (target/relative).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise ValueError('Unsafe archive path')
            if name.endswith('/'):
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(archive.read(name))


def patch_xhs(source):
    for relative in ('source/module/manager.py', 'source/application/request.py'):
        path = source/relative
        path.write_text(path.read_text(encoding='utf-8').replace('verify=False', 'verify=True'), encoding='utf-8')
    path = source/'source/application/explore.py'
    text = path.read_text(encoding='utf-8')
    old = 'return _("视频") if len(list_) == 1 else _("图集")'
    if old in text:
        text = text.replace(old, 'return _("视频")')
    elif 'return _("视频")' not in text:
        raise ValueError('XHS upstream classification changed; review the patch')
    path.write_text(text, encoding='utf-8')


def prepare_xhs_guest(target):
    source = target/'guest-source'
    marker = source/'upstream.json'
    provenance = dict(upstream='Cloxl/xhshow', commit=XHSHOW, license='MIT')
    if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != provenance:
        source_archive(provenance['upstream'], XHSHOW, source)
        marker.write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    runtime = target/'runtime'
    subprocess.run([str(runtime/'Scripts/python.exe'), '-m', 'pip', 'install',
        '--index-url', 'https://pypi.org/simple', 'pycryptodome==3.23.0'],check=True)
    package = runtime/'Lib/site-packages/xhshow'
    shutil.copytree(source/'src/xhshow',package,dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copy2(source/'LICENSE',package/'LICENSE')
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-verified', action='store_true')
    args = parser.parse_args()
    root = Path(os.environ['LOCALAPPDATA'])/'Prism/components'
    verified = Path.home()/'.agent-reach/tools/prism-download'
    definitions = [
        ('video-f2', verified/'f2-env', dict(upstream='Johnserf-Seed/f2', commit=F2, license='Apache-2.0')),
        ('video-xhs', verified/'xhs-env', dict(upstream='JoeanAmier/XHS-Downloader', commit=XHS, license='GPL-3.0')),
        ('video-bili', Path(os.environ['APPDATA'])/'uv/tools/bilibili-cli',
         dict(upstream='Nemo2011/bilibili-api', version='17.4.2', license='GPL-3.0-or-later')),
    ]
    for name, existing, manifest in definitions:
        target = root/name
        runtime = target/'runtime'
        target.mkdir(parents=True, exist_ok=True)
        if not (runtime/'Scripts/python.exe').exists():
            venv.EnvBuilder(with_pip=True).create(runtime)
        python = str(runtime/'Scripts/python.exe')
        if args.from_verified:
            shutil.copytree(existing/'Lib/site-packages', runtime/'Lib/site-packages', dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'direct_url.json'))
        if name == 'video-f2':
            source = target/'upstream-source'
            if args.from_verified:
                shutil.copytree(verified/'f2/f2', source/'f2', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                for filename in ('LICENSE', 'README.md'):
                    shutil.copy2(verified/'f2'/filename, source/filename)
                # Editable installs contain local .pth paths. Put the pinned
                # package itself into this runtime instead.
                shutil.copytree(source/'f2', runtime/'Lib/site-packages/f2', dirs_exist_ok=True)
            else:
                source_archive(manifest['upstream'], F2, source)
                subprocess.run([python, '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', str(source), 'curl-cffi==0.16.3'], check=True)
        elif name == 'video-xhs':
            source = target/'upstream-source'
            if args.from_verified:
                for folder in ('source', 'locale', 'static'):
                    if (verified/'XHS-Downloader'/folder).exists():
                        shutil.copytree(verified/'XHS-Downloader'/folder, source/folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                for filename in ('LICENSE', 'README.md', 'requirements.txt', 'pyproject.toml'):
                    if (verified/'XHS-Downloader'/filename).exists():
                        shutil.copy2(verified/'XHS-Downloader'/filename, source/filename)
            else:
                source_archive(manifest['upstream'], XHS, source)
                subprocess.run([python, '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', '-r', str(source/'requirements.txt')], check=True)
            patch_xhs(source)
            manifest['guest_session'] = prepare_xhs_guest(target)
            manifest['patches'] = ['preserve video type for multiple cover frames', 'TLS verification enabled']
        elif not args.from_verified:
            subprocess.run([python, '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', 'bilibili-api-python==17.4.2', 'aiohttp'], check=True)
        # No persistent pointers to the developer's editable installations.
        for path in (runtime/'Lib/site-packages').glob('*.pth'):
            if path.name.startswith('__editable__'):
                path.unlink()
        (target/'component.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print('Prepared '+name, flush=True)


if __name__ == '__main__':
    main()
