"""Read-only Eagle adapter to the existing Prism writer and managed storage.

No widgets, players or renderers are created during conversion. The temporary
archive uses the normal writer; managed publication verifies the originals.
"""
from __future__ import annotations

import hashlib
import base64
import io
import json
import math
import re
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from prism.media_types import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from prism.storage.migration import _check


@dataclass(frozen=True)
class ConversionResult:
    entry: Path
    folders: int
    images: int
    videos: int
    models: int
    deleted: int
    attachments: int


@dataclass(frozen=True)
class CurrentProjectImport:
    plan: object
    metadata: dict
    models: tuple
    folder_paths: dict


def prepare_current_import(source, *, cancel=None, progress=None):
    """Prepare the standard resource-import plan without touching the project."""
    from prism.fileio.resource_import import ImportNode, ImportPlan, node_title
    from prism.model_library.contracts import MODEL_EXTENSIONS
    root = library_root(source)
    library = _json(root / 'metadata.json')
    title = node_title(root.name.removesuffix('.library'))
    nodes = [ImportNode('eagle', None, 'folder', title, 'canvas')]
    pages, paths, assets, metadata, models, skipped = {}, {}, defaultdict(list), {}, [], []

    def folders(entries, parent='eagle', titles=()):
        for folder in entries:
            key = str(folder['id'])
            if key in pages:
                raise ValueError('Eagle 文件夹 ID 重复')
            name = node_title(folder.get('name') or '未命名')
            children = folder.get('children') or []
            container = parent
            if children:
                container = 'folder:' + key
                nodes.append(ImportNode(container, parent, 'folder', name, 'canvas'))
            page = 'canvas:' + key
            pages[key] = page
            paths[key] = (*titles, (key,name))
            nodes.append(ImportNode(page, container, 'page', name, 'canvas'))
            folders(children, container, (*titles, (key,name)))

    folders(library.get('folders') or [])
    for index, path in enumerate(sorted((root / 'images').glob('*.info/metadata.json'))):
        _check(cancel)
        if not path.parent.resolve().is_relative_to(root / 'images'):
            raise ValueError('Eagle 条目路径超出资料库')
        meta = _json(path)
        original = _source(path.parent, meta)
        folder_ids = list(dict.fromkeys(meta.get('folders') or []))
        if any(key not in pages for key in folder_ids):
            raise ValueError(f'Eagle 条目引用不存在的文件夹：{meta.get("id")}')
        if original.suffix.lower() in MODEL_EXTENSIONS:
            models.append((str(original), meta, folder_ids))
        elif original.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            targets = [pages[key] for key in folder_ids] or ['unclassified']
            for target in targets:
                assets[target].append(str(original))
            data = dict(title=str(meta.get('name') or ''), notes=str(meta.get('annotation') or ''),
                tags=meta.get('tags') or [], rating=max(0,min(5,int(meta.get('star') or 0))))
            data.update(_saved_previews(original))
            if original.suffix.lower() in VIDEO_EXTENSIONS:
                data.update(videoWidth=int(meta.get('resolutionWidth') or meta.get('width') or 320),
                            videoHeight=int(meta.get('resolutionHeight') or meta.get('height') or 240))
            if meta.get('isDeleted'):
                data['deletedAt'] = int(meta.get('modificationTime') or time.time()*1000)
            metadata[str(original)] = data
        else:
            skipped.append((str(original), '当前项目的素材导入不支持此格式；原件未修改'))
        if progress:
            progress({'phase':'eagle_scan', 'processed':index+1})
    if assets.get('unclassified'):
        nodes.append(ImportNode('unclassified', 'eagle', 'page', '未分类', 'canvas'))
    from dataclasses import replace
    nodes = tuple(replace(node, assets=tuple(assets[node.relative_path])) for node in nodes)
    return CurrentProjectImport(ImportPlan('canvas',title,nodes,skipped=tuple(skipped)),
                                metadata,tuple(models),paths)


def library_root(path):
    """Accept a library or its single wrapping directory from an extraction."""
    root = Path(path).resolve()
    if not (root / 'metadata.json').is_file():
        children = [p for p in root.iterdir() if p.is_dir()
                    and (p / 'metadata.json').is_file() and (p / 'images').is_dir()]
        if len(children) != 1:
            raise ValueError('请选择包含 metadata.json 和 images 的 Eagle .library 文件夹')
        root = children[0].resolve()
    if not (root / 'images').is_dir():
        raise ValueError('Eagle 资料库缺少 images 文件夹')
    return root


def _json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _source(directory, meta):
    # Never allow a metadata filename or symlink to escape this item's folder.
    name, ext = str(meta.get('name', '')), str(meta.get('ext', '')).lstrip('.')
    candidate = directory / f'{name}.{ext}'
    if not candidate.is_file():
        candidates = [p for p in directory.iterdir() if p.is_file()
                      and p.suffix.lower() == '.' + ext.lower()
                      and not p.stem.endswith('_thumbnail')]
        if len(candidates) != 1:
            raise ValueError(f'Eagle 原件缺失或无法确定：{directory.name}')
        candidate = candidates[0]
    candidate = candidate.resolve()
    if candidate.parent != directory.resolve():
        raise ValueError(f'Eagle 原件路径超出条目文件夹：{directory.name}')
    return candidate


def _stamp(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _digest(path, cancel):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            _check(cancel)
            value.update(chunk)
    return value.hexdigest()


def _saved_previews(original):
    """Reuse Eagle's poster and Prism's existing saved-thumbnail wire format."""
    from PIL import Image, ImageOps
    poster = original.with_name(original.stem + '_thumbnail.png')
    source = poster if poster.is_file() else original if original.suffix.lower() in IMAGE_EXTENSIONS else None
    if source is None:
        return {}
    try:
        with Image.open(source) as image:
            image.draft('RGB', (192,192))
            image.thumbnail((192,192), Image.Resampling.LANCZOS)
            image = ImageOps.exif_transpose(image).convert('RGBA')
            result = {}
            for key, side in (('displayThumbnail', 192), ('displayOverview', 8)):
                preview = image.copy()
                preview.thumbnail((side, side), Image.Resampling.LANCZOS)
                if key == 'displayOverview':
                    blob = preview.tobytes()  # Existing overview format is raw RGBA.
                else:
                    output = io.BytesIO()
                    preview.save(output, format='PNG')
                    blob = output.getvalue()
                result[key] = [*preview.size, base64.b64encode(blob).decode('ascii')]
            return result
    except (OSError, ValueError):
        # The original is still preserved; normal async preview generation can
        # handle formats Pillow cannot decode. No substitute original is used.
        return {}


def _title(tree, section, title, parent, node_type):
    """Keep Eagle's original name in the manifest if Prism needs a safe alias."""
    title = re.sub(r'[\x00-\x1f/\\]', '_', str(title)).strip()[:80] or '未命名'
    names = {n['title'] for n in tree.nodes if n['section'] == section
             and n['parentId'] == parent and n['nodeType'] == node_type}
    base, number = title, 2
    while title in names:
        suffix = f' ({number})'
        title = base[:80-len(suffix)] + suffix
        number += 1
    return title


def convert_library(source, destination, *, cancel=None, progress=None):
    """Create a new project, never overwrite a project or modify Eagle files."""
    from prism.fileio.save_preparation import CapturedItem
    from prism.fileio.sql import SQLiteIO
    from prism.model_library.contracts import MODEL_EXTENSIONS
    from prism.storage.entrypoint import convert, save_as_entry
    from prism.workspace_service import ResourceTreeService

    root = library_root(source)
    entry = save_as_entry(destination).resolve()
    if entry.is_relative_to(root) or root.is_relative_to(entry.with_name(entry.name+'.data')):
        raise ValueError('请将新项目保存在 Eagle 资料库之外')
    if entry.exists() or entry.with_name(entry.name+'.data').exists():
        raise FileExistsError(f'目标项目已存在，未覆盖：{entry}')
    entry.parent.mkdir(parents=True, exist_ok=True)
    report = progress or (lambda value: None)
    root_meta = _json(root / 'metadata.json')
    records = []
    for path in sorted((root / 'images').glob('*.info/metadata.json')):
        _check(cancel)
        directory = path.parent.resolve()
        if not directory.is_relative_to(root / 'images'):
            raise ValueError('Eagle 条目路径超出资料库')
        meta = _json(path)
        original = _source(directory, meta)
        stamp = _stamp(original)
        digest = _digest(original, cancel)
        if _stamp(original) != stamp:
            raise ValueError(f'转换期间原件发生变化：{directory.name}')
        records.append((meta, original, stamp, digest))
        report({'phase': 'eagle_scan', 'processed': len(records)})

    project_id = uuid.uuid4().hex
    tree = ResourceTreeService(project_id, model_section_enabled=True)
    pages = [{'id': 'default-canvas', 'kind': 'canvas', 'title': '默认画布',
              'content': None, 'tags': [], 'parent': None, 'permanent': True}]
    tree.createPage('canvas', '默认画布', 'default-canvas')
    canvases, model_folders = {}, {}

    def folders(values, parent=None, model_parent=None):
        for folder in values:
            _check(cancel)
            key = str(folder['id'])
            if key in canvases:
                raise ValueError('Eagle 文件夹 ID 重复')
            name = folder.get('name', '未命名')
            children = folder.get('children', []) or []
            # Only branches need a Prism container; every Eagle folder gets a canvas.
            container = parent
            if children:
                container = tree.createFolder('canvas',
                    _title(tree, 'canvas', name, parent, 'folder'), parent)['id']
            page_id = uuid.uuid4().hex
            title = _title(tree, 'canvas', name, container, 'page')
            tree.createPage('canvas', title, page_id, container)
            pages.append({'id': page_id, 'kind': 'canvas', 'title': title,
                          'content': None, 'tags': folder.get('tags', []),
                          'parent': container, 'permanent': False})
            canvases[key] = page_id
            # The model section is populated lazily, only if a model needs it.
            model_folders[key] = (folder, model_parent)
            folders(children, container, key)

    folders(root_meta.get('folders', []) or [])
    folder_count = len(canvases)
    offsets = defaultdict(int)
    layout_items = defaultdict(list)
    models, extra = [], []
    counts = defaultdict(int)
    manifest = {'format': 1, 'source': str(root), 'library': root_meta,
                'folder_canvases': canvases, 'items': []}
    scene = SimpleNamespace(project_id=project_id, workspace_pages=pages,
        workspace_nodes=tree.nodes, category_names=[], tag_names=[],
        library_queries=[], material_records=[], model_records=[],
        _prism_archive_items=set(), attachments={})
    scene.items_for_save = lambda: ()
    scene.items_by_type = lambda kind: ()
    scene.clear_save_ids = lambda: None

    with tempfile.TemporaryDirectory(prefix='prism-eagle-') as temporary:
        staging = Path(temporary) / 'eagle.prism'
        writer = SQLiteIO(str(staging), scene, create_new=True)
        try:
            writer.create_schema_on_new()
            writer.write_board_extras()
            writer._batch_write = True
            for index, (meta, original, stamp, digest) in enumerate(records):
                _check(cancel)
                if _stamp(original) != stamp:
                    raise ValueError(f'Eagle 原件已变化：{original.name}')
                ext = original.suffix.lower()
                deleted = bool(meta.get('isDeleted'))
                counts['deleted'] += int(deleted)
                mapping = {'metadata': meta, 'sha256': digest, 'prism_items': []}
                manifest['items'].append(mapping)
                folder_ids = list(dict.fromkeys(meta.get('folders', []) or []))
                unknown = [key for key in folder_ids if key not in canvases]
                if unknown:
                    raise ValueError(f'Eagle 条目引用不存在的文件夹：{meta.get("id")} / {unknown}')
                targets = [canvases[key] for key in folder_ids] or ['default-canvas']
                if ext in MODEL_EXTENSIONS:
                    models.append((meta, original, folder_ids, mapping))
                    counts['models'] += 1
                    continue
                if ext not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
                    extra.append((meta, original, mapping))
                    counts['attachments'] += 1
                    continue
                kind = 'video' if ext in VIDEO_EXTENSIONS else 'pixmap'
                previews = _saved_previews(original)
                counts['videos' if kind == 'video' else 'images'] += 1
                for canvas in targets:
                    number = offsets[canvas]
                    offsets[canvas] += 1
                    width = max(1, int(meta.get('width') or meta.get('resolutionWidth') or 320))
                    height = max(1, int(meta.get('height') or meta.get('resolutionHeight') or 240))
                    item = CapturedItem()
                    item.TYPE, item.save_id = kind, None
                    item.geometry = (0, 0,
                                     number, min(320/width, 320/height, 1), 0, 1)
                    item.filename = original.name
                    item._source_path = str(original) if kind == 'pixmap' else None
                    item._source_blob = item._prism_source = item._video_blob = item._glb_blob = None
                    item._source_format = ext.lstrip('.')
                    item._video_source_path = str(original) if kind == 'video' else None
                    item._is_reference = False
                    item._generated_source = (None, None)
                    item.data = {'filename': original.name, 'title': str(meta.get('name', '')),
                        'tags': meta.get('tags', []) or [], 'rating': max(0, min(5, int(meta.get('star') or 0))),
                        'notes': str(meta.get('annotation') or ''), 'canvasId': canvas,
                        'imageWidth': width, 'imageHeight': height, 'eagle': meta}
                    item.data.update(previews)
                    if kind == 'video':
                        item.data.update(videoWidth=width, videoHeight=height,
                                         isReference=False, loop=True, mute=True)
                    if deleted:
                        item.data['deletedAt'] = int(meta.get('modificationTime') or time.time()*1000)
                    writer.insert_item(item)
                    if not deleted:
                        scale = item.geometry[3]
                        layout_items[canvas].append((item.save_id,
                            math.ceil(width*scale)+12, math.ceil(height*scale)+12))
                    # The normal image writer owns this set; release each private
                    # projection so a large library never retains all source blobs.
                    scene._prism_archive_items.discard(item)
                    rowid = writer.fetchone('SELECT rowid FROM sqlar WHERE item_id=?', (item.save_id,))[0]
                    with writer.connection.blobopen('sqlar', 'data', rowid, readonly=True) as stream:
                        copied_hash = hashlib.sha256()
                        for chunk in iter(lambda: stream.read(1024*1024), b''):
                            _check(cancel)
                            copied_hash.update(chunk)
                        copied = copied_hash.hexdigest()
                    if copied != digest or _stamp(original) != stamp:
                        raise ValueError(f'原件复制校验失败：{original.name}')
                    mapping['prism_items'].append(item.save_id)
                report({'phase': 'eagle_copy', 'processed': index+1, 'total': len(records)})
            from prism.compact_layout import pack_sizes
            for entries in layout_items.values():
                positions, _ = pack_sizes([(w,h) for _,w,h in entries], cancelled=cancel)
                writer.exmany('UPDATE items SET x=?,y=? WHERE id=?',
                    [(x+6,y+6,entry[0]) for entry,(x,y) in zip(entries,positions)])
            writer.connection.commit()
        finally:
            writer._close_connection()

        if models:
            _import_models(staging, models, tree, model_folders, cancel)
        for _, original, stamp, _ in records:
            if _stamp(original) != stamp:
                raise ValueError(f'转换期间 Eagle 原件发生变化：{original.name}')
        # Preserve non-canvas formats as regular project attachments, with an
        # explicit manifest. Never silently discard a source file.
        import sqlite3
        with closing(sqlite3.connect(staging)) as connection, connection:
            for meta, original, mapping in extra:
                _check(cancel)
                size = original.stat().st_size
                cursor = connection.execute('INSERT INTO attachments(board_id,name,mime,blob,created_at) '
                    'VALUES(0,?,?,zeroblob(?),?)', (original.name, 'application/octet-stream', size, int(time.time())))
                digest = hashlib.sha256()
                with connection.blobopen('attachments', 'blob', cursor.lastrowid) as output, original.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024*1024), b''):
                        _check(cancel)
                        output.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != mapping['sha256']:
                    raise ValueError(f'附件原件复制校验失败：{original.name}')
                mapping['attachment_id'] = cursor.lastrowid
            connection.execute('INSERT INTO attachments(board_id,name,mime,blob,created_at) VALUES(0,?,?,?,?)',
                ('Eagle转换记录.json', 'application/json',
                 json.dumps(manifest, ensure_ascii=False).encode('utf-8'), int(time.time())))
            connection.executemany('INSERT OR REPLACE INTO workspace_nodes(id,payload) VALUES(?,?)',
                [(n['id'], json.dumps(n, ensure_ascii=False)) for n in tree.nodes])
            connection.execute('INSERT OR REPLACE INTO prism_metadata(key,value) VALUES(?,?)',
                ('tags', json.dumps(sorted({str(tag) for m, *_ in records for tag in (m.get('tags') or [])}), ensure_ascii=False)))
        _check(cancel)
        published = convert(staging, entry, cancel=cancel, progress=progress)
    return ConversionResult(published, folder_count, counts['images'], counts['videos'],
                            counts['models'], counts['deleted'], counts['attachments'])


def _import_models(staging, models, tree, folders, cancel):
    """Use the existing model importer, repository and original-byte checks."""
    from prism.model_library.importer import ModelImporter
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    context = open_model_context(staging)
    repository = ModelRepositoryAdapter(context)
    events = []
    importer = ModelImporter(context, repository, publish_event=events.append)
    installed = {}

    def model_folder(key):
        if key is None:
            return None
        if key not in installed:
            folder, parent = folders[key]
            parent_id = model_folder(parent)
            title = _title(tree, 'model', folder['name'], parent_id, 'folder')
            installed[key] = tree.createFolder('model', title, parent_id)['id']
        return installed[key]

    try:
        for meta, source, folder_ids, mapping in models:
            _check(cancel)
            events.clear()
            job = importer.import_paths([str(source)], folder_id=model_folder(next(iter(folder_ids), None)))
            while importer.job_state(job) not in ('completed', 'failed', 'cancelled'):
                if cancel and cancel():
                    importer.cancel(job)
                time.sleep(.02)
            _check(cancel)
            if importer.job_state(job) != 'completed':
                failures = [str(e.failure.message) for e in events if hasattr(e, 'failure')]
                raise ValueError('Eagle 模型转换失败：' + '; '.join(failures))
            event = next(e for e in events if hasattr(e, 'result'))
            payload = repository.get_payload(event.result.model_id)
            revision = payload['record_revision']
            payload.update(name=meta['name'], tags=meta.get('tags') or [],
                           notes=str(meta.get('annotation') or ''), eagle=meta,
                           rating=max(0, min(5, int(meta.get('star') or 0))))
            if meta.get('isDeleted'):
                payload['deleted_at'] = int(meta.get('modificationTime') or time.time()*1000)
            repository.update(payload, expected_revision=revision)
            mapping['model_id'] = event.result.model_id
    finally:
        importer.close_project(context.session_token)
