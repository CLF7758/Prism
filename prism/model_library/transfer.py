"""Persistent model/texture file handoff; no renderer or UE plugin required."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from .exporter import _files_with_material_textures
from .events import record_model_event
from prism.material_transfer import _asset_key

_LOCKS = tuple(threading.Lock() for _ in range(32))
_IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.tga', '.bmp', '.tif', '.tiff',
                               '.exr', '.hdr', '.dds', '.psd', '.webp', '.avif', '.gif'})


def transfer_directory():
    from prism.config import PrismSettings
    return Path(PrismSettings().fileName()).parent / 'model-transfers'


def _relative(value):
    relative = Path(str(value).replace(chr(92), '/'))
    if relative.is_absolute() or '..' in relative.parts or ':' in str(relative) or not relative.name:
        raise ValueError('模型依赖路径不安全')
    return relative


def prepare_model_transfer(service, model_ids, directory=None):
    """Worker-only preparation reuses frozen metadata and verified originals.

    Keep buffers/MTL alongside source models, but hand the OS only models and
    texture files. Persistent paths retain UE's source reimport references.
    """
    root = Path(directory or transfer_directory()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    plans, missing = [], []
    for model_id in dict.fromkeys(model_ids):
        if service._closed:
            raise ValueError('工程会话已关闭')
        payload = service.repository.get_payload(model_id)
        if payload is None or payload.get('deleted_at'):
            raise ValueError('选中的模型已删除或当前工程已切换')
        files = _files_with_material_textures(payload)
        main = next((file for file in files if file.get('role') == 'main'), None)
        if main is None or main.get('missing'):
            raise ValueError('模型原件缺失，无法拖出')
        for file in files:
            _relative(file['relative_path'])
        missing.extend(file.get('relative_path', '') for file in files if file.get('missing'))
        plans.append((model_id, [file for file in files if not file.get('missing')]))
    if not plans:
        raise ValueError('没有选中模型')
    candidates = [(key, file) for key, files in plans for file in files
                  if file.get('role') == 'main' or file.get('role') == 'texture'
                  or Path(file['relative_path']).suffix.lower() in _IMAGE_EXTENSIONS]
    names = {}
    for _, file in candidates:
        names.setdefault(_asset_key(_relative(file['relative_path']).name), set()).add(file['sha256'])
    paths, sent_textures, texture_count = [], set(), 0
    for model_id, files in plans:
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        package = root / (hashlib.sha256(model_id.encode()).hexdigest()[:16] + '-' + digest)
        with _LOCKS[hash(str(package)) % len(_LOCKS)]:
            package.mkdir(parents=True, exist_ok=True)
            stamp = package / '.verified.json'
            try:
                verified = json.loads(stamp.read_text(encoding='utf-8'))
                if not isinstance(verified, dict):
                    verified = {}
            except (OSError, ValueError):
                verified = {}
            def ensure(file, relative):
                target = package / relative
                proof = verified.get(relative.as_posix())
                try:
                    stat = target.stat()
                    if proof == [file['sha256'], stat.st_size, stat.st_mtime_ns]:
                        return target
                except OSError:
                    pass
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.prepare-', dir=package) as temporary:
                    staged = Path(temporary)/'original'
                    service.exporter._copy_verified(file, staged)
                    os.replace(staged, target)
                stat = target.stat()
                verified[relative.as_posix()] = [file['sha256'], stat.st_size, stat.st_mtime_ns]
                return target
            for file in files:
                ensure(file, _relative(file['relative_path']))
            for key, file in candidates:
                if key != model_id:
                    continue
                is_texture = file.get('role') != 'main'
                if is_texture and file['sha256'] in sent_textures:
                    continue
                relative = _relative(file['relative_path'])
                if len(names[_asset_key(relative.name)]) > 1:
                    relative = relative.with_name(relative.stem + '_' + file['sha256'][:16] + relative.suffix)
                paths.append(str(ensure(file, relative)))
                if is_texture:
                    sent_textures.add(file['sha256'])
                    texture_count += 1
            temporary_stamp = stamp.with_suffix('.tmp')
            temporary_stamp.write_text(json.dumps(verified), encoding='utf-8')
            os.replace(temporary_stamp, stamp)
    record_model_event('external_drag_prepared', count=len(plans), textures=texture_count,
                       files=len(paths), missing=len(missing))
    return dict(paths=paths, textures=texture_count, missing=missing)
