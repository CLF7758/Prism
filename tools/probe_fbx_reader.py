"""Check the FBX reader against the real sample in .tmp/f3d_probe."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prism.model_library.importer import parse_fbx_references

source = Path(__file__).resolve().parents[1] / '.tmp' / 'f3d_probe' / 'box.fbx'
print('exists:', source.exists())
if source.exists():
    references = parse_fbx_references(source)
    print('references:', len(references))
    for reference in references[:20]:
        print(' ', reference.role, '|', reference.resolution, '|',
              reference.detail, '|', reference.reference[:80], '|',
              reference.path)
