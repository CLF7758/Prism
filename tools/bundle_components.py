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
from importlib import metadata, util


def copy_tree(source, target, exclude=()):
    shutil.copytree(source, target, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git', 'direct_url.json', *exclude))


def copy_storage_dependencies(target):
    """External visual workers cannot import pure modules from the EXE's PYZ."""
    for module, distribution in (('disk_objectstore','disk-objectstore'),
            ('sqlalchemy','SQLAlchemy'), ('click','click'), ('greenlet','greenlet'),
            ('typing_extensions','typing_extensions')):
        spec = util.find_spec(module)
        if spec is None or not spec.origin:
            if module == 'greenlet':
                continue  # SQLAlchemy's optional async adapter is not used here.
            raise RuntimeError('Missing visual storage dependency: '+module)
        source = Path(spec.origin)
        if spec.submodule_search_locations:
            copy_tree(source.parent, target/module)
        else:
            shutil.copy2(source, target/source.name)
        dist = metadata.distribution(distribution)
        for entry in dist.files or ():
            if any(part.endswith('.dist-info') for part in entry.parts) and entry.name != 'direct_url.json':
                original = Path(dist.locate_file(entry))
                if original.is_file():
                    destination = target/entry
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, destination)


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
    for name in ('capture', 'visual-search', 'video-download', 'video-f2', 'video-xhs', 'video-bili'):
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
            copy_storage_dependencies(dst/'runtime/Lib/site-packages')
            weight=src/'index/models/ViT-B-32.pt'
            with weight.open('rb') as f:
                assert hashlib.file_digest(f,'sha256').hexdigest() == '40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af'
            (dst/'index/models').mkdir(parents=True)
            shutil.copy2(weight,dst/'index/models'/weight.name)
        elif name == 'video-download':
            copy_tree(src/'bin',dst/'bin')
        elif (src/'upstream-source').is_dir():
            copy_tree(src/'upstream-source', dst/'upstream-source', exclude=('Volume','Temp','.env','*.log','cookies.txt'))
        if name == 'video-xhs':
            copy_tree(src/'guest-source', dst/'guest-source')
        print('Staged '+name, flush=True)
    print('Bundled bytes: '+str(sum(p.stat().st_size for p in out.rglob('*') if p.is_file())),flush=True)


if __name__ == '__main__':
    main()
