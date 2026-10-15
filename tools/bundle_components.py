"""Stage installed, isolated components without user indexes, settings or cookies.
Run using the Python 3.12 base interpreter used to prepare the components.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


def copy_tree(source, target):
    shutil.copytree(source, target, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git', 'direct_url.json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--components', type=Path,
        default=Path(os.environ['LOCALAPPDATA'])/'Prism/components')
    parser.add_argument('--output', type=Path,
        default=Path(__file__).resolve().parents[1]/'build/bundled-components')
    args = parser.parse_args()
    out = args.output.resolve()
    if out.exists():
        raise RuntimeError('Use a fresh staging directory: '+str(out))
    out.mkdir(parents=True)
    base = Path(sys.base_prefix)
    target = out/'python-base'
    target.mkdir()
    for path in base.iterdir():
        if path.is_file() and path.suffix.lower() in ('.exe', '.dll', '.txt'):
            shutil.copy2(path, target/path.name)
    copy_tree(base/'DLLs', target/'DLLs')
    shutil.copytree(base/'Lib', target/'Lib',
        ignore=shutil.ignore_patterns('site-packages', '__pycache__', '*.pyc', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip'))
    for name in ('capture', 'visual-search', 'video-download'):
        src = args.components/name
        dst = out/name
        (dst/'runtime/Scripts').mkdir(parents=True)
        shutil.copy2(src/'runtime/Scripts/python.exe', dst/'runtime/Scripts/python.exe')
        (dst/'runtime/pyvenv.cfg').write_text('home = ../../python-base\ninclude-system-site-packages = false\nversion = 3.12\n', encoding='utf-8')
        copy_tree(src/'runtime/Lib/site-packages', dst/'runtime/Lib/site-packages')
        for filename in ('component.json', 'binaries.json'):
            if (src/filename).is_file():
                data=json.loads((src/filename).read_text(encoding='utf-8'))
                # Manifests are provenance only; do not retain local absolute paths.
                data={k:v for k,v in data.items() if not isinstance(v,str) or ':\\' not in v}
                (dst/filename).write_text(json.dumps(data,indent=2),encoding='utf-8')
        if name == 'capture':
            copy_tree(src/'upstream-source/src', dst/'upstream-source/src')
            for filename in ('LICENSE', 'README.md'):
                if (src/'upstream-source'/filename).is_file():
                    shutil.copy2(src/'upstream-source'/filename,dst/'upstream-source'/filename)
            data=json.loads((src/'upstream.json').read_text(encoding='utf-8'))
            data['source']='upstream-source/src'
            (dst/'upstream.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
        elif name == 'visual-search':
            weight=src/'index/models/ViT-B-32.pt'
            with weight.open('rb') as f:
                assert hashlib.file_digest(f,'sha256').hexdigest() == '40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af'
            (dst/'index/models').mkdir(parents=True)
            shutil.copy2(weight,dst/'index/models'/weight.name)
        else:
            copy_tree(src/'bin',dst/'bin')
        print('Staged '+name, flush=True)
    print('Bundled bytes: '+str(sum(p.stat().st_size for p in out.rglob('*') if p.is_file())),flush=True)


if __name__ == '__main__':
    main()
