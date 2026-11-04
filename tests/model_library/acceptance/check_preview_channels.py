"""Real F3D offscreen smoke, never creates or controls a desktop window.

Run with .venv/Scripts/python.exe -m tests.model_library.acceptance.check_preview_channels
or execute this path with repository root on PYTHONPATH.
"""
import f3d  # Native DLLs must precede Qt imports.
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from prism.components.model_worker import ENVIRONMENT
from prism.model_library.preview_channels import modes_for, read_gltf


def main():
    source = Path('E:/Prism-stress-100k')
    out = ROOT / 'artifacts/model-preview-ui-20261027'
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    engine = f3d.Engine.create(offscreen=True)
    from PyQt6.QtWidgets import QApplication
    from prism.components.model_preview import build_viewport_class
    app = QApplication([])
    viewport = build_viewport_class()()  # Never shown; renders only via offscreen F3D.
    viewport.engine = engine
    viewport.channel_cache_root = out / 'cache'
    errors = []
    viewport.native_error.connect(lambda code, message: errors.append((code, message)))
    engine.window.size = (384, 384)
    for key, value in ENVIRONMENT.items():
        engine.options[key] = value
    results = []
    for index in (1, 3, 9, 15):
        record = manifest['models'][index - 1]
        candidates = list((source / record['folder']).rglob('*.' + {1: 'fbx', 3: 'gltf', 9: 'glb', 15: 'gltf'}[index]))
        path = next(p for p in candidates if '_collection' not in str(p))
        original_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in path.parent.rglob('*') if p.is_file()}
        viewport._apply_request({'model_path': str(path)})
        assert not errors, errors
        camera = engine.window.camera
        saved = (camera.position, camera.focal_point, camera.view_up, camera.view_angle)
        baseline = out / ('%02d-full.png' % index)
        engine.window.render_to_image().save(str(baseline))
        rows = []
        for mode, reason in modes_for(path).items():
            if reason:
                rows.append({'mode': mode, 'status': 'unavailable', 'reason': reason})
                continue
            try:
                assert viewport.set_mode(mode), errors
                output = out / ('%02d-%s.png' % (index, mode))
                engine.window.render_to_image().save(str(output))
                with Image.open(output) as image, Image.open(baseline) as before:
                    changed = ImageChops.difference(image.convert('RGB'), before.convert('RGB')).getbbox() is not None
                    nonempty = image.convert('RGB').getbbox() is not None
                if not nonempty:
                    materials = read_gltf(path)[0].get('materials', [])
                    expected_zero = ((mode == 'metallic' and all(m.get('pbrMetallicRoughness', {}).get('metallicFactor', 1) == 0 for m in materials))
                        or (mode == 'emissive' and all(not any(m.get('emissiveFactor', [0, 0, 0])) for m in materials)))
                    assert expected_zero, 'unexpected empty channel frame'
                rows.append({'mode': mode, 'status': 'rendered', 'different_from_full': changed,
                             'zero_channel': not nonempty, 'output': output.name})
            except Exception as error:
                rows.append({'mode': mode, 'status': 'failed', 'error': str(error)})
        assert viewport.set_mode('material'), errors
        restored = out / ('%02d-restored.png' % index)
        engine.window.render_to_image().save(str(restored))
        with Image.open(restored) as image, Image.open(baseline) as before:
            identical = ImageChops.difference(image.convert('RGB'), before.convert('RGB')).getbbox() is None
        assert identical, 'channel view changed restored full material frame'
        viewport.apply_snapshot({'has_override': True, 'color_rgb': [1., 0., 0.]})
        assert viewport.set_mode('normal'), errors
        viewport._apply_request({'model_path': str(path)})
        reopened = out / ('%02d-reopened.png' % index)
        engine.window.render_to_image().save(str(reopened))
        with Image.open(reopened) as image, Image.open(baseline) as before:
            assert ImageChops.difference(image.convert('RGB'), before.convert('RGB')).getbbox() is None
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest
                   for p, digest in original_hashes.items())
        results.append({'model': str(path), 'full_restored_identically': identical,
                        'reopen_clears_previous_overrides': True,
                        'source_files_unchanged': True, 'modes': rows})
        (out / 'native-channel-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(index, [(row['mode'], row['status']) for row in rows], flush=True)
    assert all(row['status'] != 'failed' for result in results for row in result['modes'])
    viewport.close_native()


if __name__ == '__main__':main()
