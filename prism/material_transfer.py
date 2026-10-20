"""Byte-exact texture files for native drag into UE; no material generation.

Persistent files survive Prism exit, retaining Unreal's source reimport paths.
"""
import hashlib
import os
from pathlib import Path
import re
import tempfile
from prism.material_textures import export_originals
from prism.performance_trace import measured, record


def transfer_directory():
    from prism.config import PrismSettings
    return Path(PrismSettings().fileName()).parent/'material-transfers'


def _asset_key(name):
    return re.sub(r'\W+', '_', Path(name).stem).casefold()


@measured('materials.drag.prepare')
def prepare_texture_transfer(records, attachments, directory):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    unique, empty = {}, 0
    for material in records:
        bound = {(binding.get('attachmentId'), binding.get('sha256'))
                 for binding in material.get('textures', {}).values() if isinstance(binding,dict)}
        files = [file for file in material.get('files', [])
                 if (file.get('attachmentId'),file.get('sha256')) in bound]
        if not files:
            empty += 1
        for file in files:
            unique.setdefault(file['sha256'], file)
    if not unique:
        raise ValueError('选中的材质没有可拖出的源贴图。')
    names = {}
    for file in unique.values():
        key = _asset_key(file['name'])
        names[key] = names.get(key,0)+1
    paths, renamed, used_names = [], 0, set()
    for sha, file in unique.items():
        name = file['name']
        if names[_asset_key(name)] > 1:
            name = Path(name).stem[:80]+'_'+sha[:16]+Path(name).suffix
        counter = 0
        while _asset_key(name) in used_names:
            counter += 1
            name = Path(file['name']).stem[:50]+'_'+sha+'_'+str(counter)+Path(file['name']).suffix
        used_names.add(_asset_key(name))
        if name != file['name']:
            renamed += 1
        target = root/sha/name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
            paths.append(target)
            continue
        # Existing original exporter owns verification and copying, not QImage.
        with tempfile.TemporaryDirectory(prefix='.prepare-',dir=root) as work:
            staged = export_originals({'files':[dict(file,name=name)]},attachments,work)[0]
            os.replace(staged,target)
        paths.append(target)
    record('material_drag_prepared', materials=len(records), textures=len(paths),
           empty_materials=empty, renamed_conflicts=renamed)
    return dict(paths=[str(path) for path in paths], materials=len(records), empty=empty, renamed=renamed)
