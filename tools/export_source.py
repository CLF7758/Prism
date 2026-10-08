"""Export the current, cleaned source tree without Git history or local data."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ['prism', 'tests', 'tools', 'packaging', 'requirements', 'docs',
               'browser-extension', '.github']
FILES = ['README.md', 'README.en.md', 'LICENSE', 'NOTICE', 'THIRD_PARTY.md',
         'CONTRIBUTING.rst', 'CHANGELOG.rst', 'Prism.spec', 'pyproject.toml',
         'setup.cfg', 'setup.py', 'MANIFEST.in', 'conftest.py', '.gitignore',
         'codecov.yml', '启动Prism源码版.bat']

def main():
    out = ROOT / 'release/Prism-0.3.5-source.zip'
    out.parent.mkdir(exist_ok=True)
    paths = [ROOT / name for name in FILES]
    for directory in DIRECTORIES:
        paths.extend((ROOT / directory).rglob('*'))
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(paths)):
            if not path.is_file() or '__pycache__' in path.parts:
                continue
            if path.suffix.lower() in ('.pyc', '.pyo', '.log', '.bak'):
                continue
            archive.write(path, 'Prism/' + path.relative_to(ROOT).as_posix())
    print(out)

if __name__ == '__main__':
    main()
