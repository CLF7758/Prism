"""Collect installed runtime dependency notices for the Windows release."""
from importlib import metadata
from pathlib import Path
import shutil
import json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'packaging/licenses'
NAMES = ['PyQt6', 'PyQt6-Qt6', 'PyQt6-WebEngine', 'PyQt6-WebEngine-Qt6',
         'PyQt6-WebEngineSubwheel-Qt6', 'PyQt6-sip', 'av', 'numpy', 'pillow',
         'lxml', 'python-docx', 'exif', 'plum-py', 'rectangle-packer',
         'OpenImageIO', 'PyYAML', 'charset-normalizer', 'packaging']

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in NAMES:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        target = OUT / name
        target.mkdir(exist_ok=True)
        copied = []
        for entry in dist.files or []:
            if any(part.lower().startswith(('license', 'copying', 'copyright', 'notice'))
                   for part in entry.parts) and entry.suffix.lower() not in ('.pyc', '.dll', '.pyd'):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    destination = target / str(entry).replace('/', '__').replace('\\', '__')
                    shutil.copy2(source, destination)
                    copied.append(destination.name)
        info = dist.metadata
        rows.append({'name': name, 'version': dist.version,
                     'license': info.get('License-Expression') or info.get('License'),
                     'project_urls': info.get_all('Project-URL') or [],
                     'notices': copied})
    (OUT / 'dependencies.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Collected notices for {len(rows)} runtime distributions')

if __name__ == '__main__':
    main()
