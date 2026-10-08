"""Export the current, cleaned source tree without Git history or local data."""
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ['prism', 'tests', 'tools', 'packaging', 'requirements', 'docs',
               'browser-extension', '.github']
FILES = ['README.md', 'README.en.md', 'LICENSE', 'NOTICE', 'THIRD_PARTY.md',
         'CONTRIBUTING.rst', 'CHANGELOG.rst', 'Prism.spec', 'pyproject.toml',
         'setup.cfg', 'setup.py', 'MANIFEST.in', 'conftest.py', '.gitignore',
         'codecov.yml', '启动Prism源码版.bat']

def main():
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    out = ROOT / f'release/Prism-{version}-source.zip'
    out.parent.mkdir(exist_ok=True)
    subprocess.run(['git', 'archive', '--format=zip', '--prefix=Prism/',
                    f'--output={out}', 'HEAD'], cwd=ROOT, check=True)
    print(out)

if __name__ == '__main__':
    main()
