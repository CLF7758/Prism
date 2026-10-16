"""Retain original bytes in the download process, never in Qt's UI."""
from pathlib import Path
import time
import re
from itertools import islice

MAX_ASSETS = 100
ASSET_SUFFIXES = frozenset(('.jpg','.jpeg','.png','.webp','.avif','.heic','.gif','.bmp','.tif','.tiff','.mp4','.mov','.webm','.mkv'))


def asset_original(request, index, path):
    suffix=Path(path).suffix.lower()
    if suffix not in ASSET_SUFFIXES or not 0 <= index < MAX_ASSETS:
        raise ValueError('不支持的素材类型或数量')
    return Path(request['original_destination']).with_suffix('')/f'{index:03}{suffix}'


def cleanup_assets(request):
    if not request.get('original_destination'):
        return
    root=Path(request['original_destination']).with_suffix('')
    if root.is_dir() and not root.is_symlink():
        for path in islice(root.iterdir(), MAX_ASSETS*2):
            name=path.name.removesuffix('.part')
            if re.fullmatch(r'\d{3}\.[a-z0-9]+',name) and Path(name).suffix in ASSET_SUFFIXES:
                path.unlink(missing_ok=True)
        try:root.rmdir()
        except OSError:pass


def complete_assets(request, assets, emit, **metadata):
    if not assets or len(assets)>MAX_ASSETS:
        raise ValueError('单次图文最多支持 100 个素材，不会截断后假报完成')
    results=[]
    try:
        for index,asset in enumerate(assets):
            item=dict(asset)
            path=Path(item['path']).resolve()
            fields=[]
            if request.get('original_destination'):
                target=asset_original(request,index,path)
                target.parent.mkdir(exist_ok=True)
                local=request | {'original_destination':str(target)}
                complete(local,path,lambda event,**value: fields.append(value) if event=='completed' else emit(event,**value))
                item['original_path']=fields[0]['original_path']
            item.update(path=str(path),bytes=path.stat().st_size)
            results.append(item)
    except Exception:
        cleanup_assets(request)
        raise
    first=results[0]
    emit('completed',path=first['path'],original_path=first.get('original_path'),
         bytes=sum(i['bytes'] for i in results),assets=results,**metadata)


def validate_assets(request, assets):
    if not isinstance(assets,list) or not assets or len(assets)>MAX_ASSETS:
        raise ValueError('素材结果数量无效')
    root=Path(request['directory']).resolve()
    for index,item in enumerate(assets):
        if item.get('asset_type') not in ('image','video'):
            raise ValueError('未知的素材类型')
        path=Path(item['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in ASSET_SUFFIXES:
            raise ValueError('素材路径无效')
        if request.get('original_destination'):
            original=Path(item.get('original_path','')).resolve()
            if original!=asset_original(request,index,path).resolve() or not original.is_file():
                raise ValueError('素材原件快照无效')
        if item['asset_type']=='image':
            preview=Path(item['preview_path']).resolve()
            size=item.get('image') or {}
            if not preview.is_relative_to(root) or not preview.is_file() or preview.stat().st_size>65536:
                raise ValueError('素材预览无效')
            if any(not isinstance(size.get(k),int) or not 0<size[k]<=100000 for k in ('width','height')):
                raise ValueError('素材尺寸无效')


def complete(request, path, emit, **metadata):
    fields = {}
    destination = request.get('original_destination')
    if destination:
        target = Path(destination)
        partial = target.with_suffix(target.suffix+'.part')
        size = path.stat().st_size
        last, done = 0, 0
        try:
            emit('progress', phase='store')
            with path.open('rb') as source, partial.open('xb') as output:
                while chunk := source.read(1024*1024):
                    output.write(chunk)
                    done += len(chunk)
                    if time.monotonic()-last >= .2:
                        emit('progress', phase='store', downloaded=done, total=size)
                        last = time.monotonic()
            if done != size:
                raise ValueError('视频原件保留不完整')
            partial.rename(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        fields['original_path'] = str(target)
    emit('completed', path=str(path.resolve()), bytes=path.stat().st_size, **metadata, **fields)
