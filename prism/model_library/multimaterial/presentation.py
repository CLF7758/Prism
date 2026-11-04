"""Detached renderer inputs. This module never imports Qt or a native renderer."""
from dataclasses import asdict
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from .catalog import catalog_to_payload
from prism.material_textures import open_decoded_texture


_CACHE_LOCKS = tuple(threading.Lock() for _ in range(64))


def _publish_cache_file(destination, write):
    """Publish complete immutable bytes; concurrent readers never see staging.

    Edit and cover threads share these content-addressed paths. A fixed .tmp
    name races on Windows, and replacing an already-open cached file can fail.
    Linking a unique completed staging file creates the destination only once.
    """
    destination=Path(destination)
    with _CACHE_LOCKS[hash(str(destination)) % len(_CACHE_LOCKS)]:
        if destination.exists():return
        destination.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix=destination.stem+'.',suffix='.tmp',dir=destination.parent)
        os.close(fd)
        temporary=Path(name)
        try:
            write(temporary)
            try:
                os.link(temporary,destination)
            except FileExistsError:
                pass  # Another process published the same immutable key.
            except OSError:
                # Filesystems without hardlinks still get a complete rename.
                if not destination.exists():
                    try:temporary.replace(destination)
                    except OSError:
                        if not destination.exists():raise
        finally:
            temporary.unlink(missing_ok=True)


def material_parameters(properties, mapped_slots=()):
    """Resolve scalar renderer values without reading files or mutating state.

    mapped_slots is the set of successfully prepared texture roles. A mapped
    roughness uses its factor; an unmapped one uses its constant value.
    """
    from prism.material_parameters import resolved_controls
    from prism.material_preview_profile import preview_profile
    slots = properties.get('slots', {})
    maps = frozenset(mapped_slots)
    controls = resolved_controls(properties, maps)
    color = controls['basecolor']['linearColor']
    if 'basecolor' not in maps and 'basecolor' not in slots:
        color = preview_profile()['untexturedColor']
    def amount(slot,default):
        options=controls[slot]
        if not options['enabled']:return default
        value=float(options['amount'])
        return 1-value if options.get('invert') and slot not in maps else value
    color=[float(c)*float(controls['basecolor']['amount']) for c in color[:3]]
    if slots.get('basecolor',{}).get('invert') and 'basecolor' not in maps:color=[1-c for c in color]
    emissive=properties.get('emissive_factor',properties.get('ke',[0,0,0]))
    if 'emissive' in slots:
        emissive=[float(c)*float(controls['emissive']['amount']) for c in controls['emissive']['linearColor']]
    if not slots.get('emissive',{}).get('enabled',True):emissive=[0,0,0]
    return dict(color=color,
        roughness=amount('roughness',.5),
        metallic=amount('metallic',0),
        opacity=float(slots.get('opacity', {}).get('value', 1)),
        alpha_mode=properties.get('alpha_mode', 'OPAQUE'),
        alpha_cutoff=float(properties.get('alpha_cutoff', .5)),
        double_sided=bool(properties.get('double_sided', False)),
        emissive_factor=emissive,
        occlusion_strength=float(slots.get('ao',{}).get('value',properties.get('occlusion_strength',1))),
        normal_scale=float(slots.get('normal',{}).get('value',properties.get('normal_scale',1))),
        unlit=bool(properties.get('unlit', False)))


def prepare_material_maps(definition, paths, properties, cache_root=None):
    """Prepare detached texture descriptors and immutable derived images.

    Original bytes are read-only. Failure propagates to the render caller and
    cannot change the effective material or project save result.
    """
    slots = properties.get('slots', {})
    maps = {}
    for use in definition.textures:
        if not slots.get(use.slot, {}).get('enabled', True):
            continue
        path = paths.get(use.resource_id)
        if path:
            options=slots.get(use.slot,{})
            channel=use.channels
            if cache_root is not None and any(options.get(k) for k in ('invert','flipX','flipY')):
                from PIL import Image, ImageOps
                digest=hashlib.sha256(json.dumps([use.resource_id,options,channel],sort_keys=True).encode()).hexdigest()
                destination=Path(cache_root)/(digest+'.preview.png')
                if not destination.exists():
                    with open_decoded_texture(Path(path).read_bytes()) as original:
                        image=original.convert('RGBA')
                        if channel in ('R','G','B','A'):
                            image=image.getchannel(channel).convert('RGBA')
                        if use.slot=='normal':
                            from prism.material_textures import transform_image
                            image=transform_image(image.convert('RGB'),'normal',{'slots':{'normal':options}}).convert('RGBA')
                        elif options.get('invert'):
                            alpha=image.getchannel('A')
                            image=ImageOps.invert(image.convert('RGB')).convert('RGBA');image.putalpha(alpha)
                        if use.slot!='normal':
                            if options.get('flipX'):image=ImageOps.mirror(image)
                            if options.get('flipY'):image=ImageOps.flip(image)
                        _publish_cache_file(destination,lambda staged:image.save(staged,format='PNG'))
                path=destination;channel='RGB'
            from prism.material_parameters import normal_convention
            convention = normal_convention(properties, use.normal_convention) if use.slot == 'normal' else use.normal_convention
            maps[use.slot] = dict(path=str(path), channels=channel,
                texcoord=use.texcoord, color_space=use.color_space,
                uv_transform=list(use.uv_transform), normal_convention=convention)
    if maps.get('opacity') and cache_root is not None:
        from PIL import Image, ImageChops
        mask=maps['opacity'];base=maps.get('basecolor')
        if base and any(base[k]!=mask[k] for k in ('texcoord','uv_transform')):
            raise ValueError('Separate opacity UV transforms require a dedicated shader')
        destination=Path(cache_root)/(hashlib.sha256(json.dumps([base,mask],sort_keys=True).encode()).hexdigest()+'.alpha.png')
        if not destination.exists():
            with open_decoded_texture(Path(mask['path']).read_bytes()) as original:
                alpha=original.convert('RGBA').getchannel(mask['channels']).copy() if mask['channels'] in ('R','G','B','A') else original.convert('L')
            if base:
                with open_decoded_texture(Path(base['path']).read_bytes()) as original:rgba=original.convert('RGBA')
            else:rgba=Image.new('RGBA',alpha.size,(255,255,255,255))
            size=(max(alpha.width,rgba.width),max(alpha.height,rgba.height))
            alpha=alpha.resize(size,Image.Resampling.BILINEAR);rgba=rgba.resize(size,Image.Resampling.BILINEAR)
            rgba.putalpha(ImageChops.multiply(rgba.getchannel('A'),alpha))
            _publish_cache_file(destination,lambda staged:rgba.save(staged,format='PNG'))
        maps['basecolor']=dict(base or mask,path=str(destination),channels='RGB',color_space='srgb')
    # Only an explicitly shared packed source may be used directly as ORM.
    packed = [maps.get(k) for k in ('ao', 'roughness', 'metallic')]
    if all(packed) and len({m['path'] for m in packed}) == 1 and [m['channels'] for m in packed] == ['R', 'G', 'B']:
        maps['orm'] = dict(packed[0], channels='RGB')
    elif any(packed) and cache_root is not None:
        from PIL import Image
        reference=next(m for m in packed if m)
        for image in packed:
            if image and any(image[k]!=reference[k] for k in ('texcoord','uv_transform')):
                raise ValueError('Independent ORM UV transforms require a dedicated shader')
        digest=hashlib.sha256(json.dumps(packed,sort_keys=True).encode()).hexdigest()
        destination=Path(cache_root)/(digest+'.orm.png')
        if not destination.exists():
            images=[]
            for image in packed:
                if image is None:
                    images.append(None)
                    continue
                with open_decoded_texture(Path(image['path']).read_bytes()) as original:
                    original.load()
                    channel=image['channels']
                    images.append(original.convert('RGBA').getchannel(channel).copy() if channel in 'RGBA' and len(channel)==1
                                  else original.convert('L'))
            size=(max(i.width for i in images if i),max(i.height for i in images if i))
            channels=[i.resize(size,Image.Resampling.BILINEAR) if i else Image.new('L',size,255) for i in images]
            combined=Image.merge('RGB',channels)
            _publish_cache_file(destination,lambda staged:combined.save(staged,format='PNG'))
        maps['orm']=dict(reference,path=str(destination),channels='RGB')
    return maps


def material_spec(definition, paths, cache_root=None):
    """Adapt effective material state to renderer input (legacy entry point)."""
    properties = json.loads(definition.properties_json or '{}')
    # Validate state before performing potentially expensive image preparation.
    material_parameters(properties)
    maps = prepare_material_maps(definition, paths, properties, cache_root)
    parameters = material_parameters(properties, maps)
    from prism.material_preview_profile import profile_signature
    return dict(parameters, maps=maps, preview_profile=profile_signature('model'))


def material_artifact(item, state, root, assimp_dll, resolved):
    # One entry per live working material; scalar edits must not prepare every
    # other material's texture maps again. Source stat identities invalidate a
    # replaced resource, while source/catalog state remains owned by the caller.
    lock = _CACHE_LOCKS[hash(str(item['path'])) % len(_CACHE_LOCKS)]
    prepared = {}
    for wid, definition in resolved.items():
        files = []
        slots = json.loads(definition.properties_json or '{}').get('slots', {})
        for use in definition.textures:
            path = item['paths'].get(use.resource_id)
            stat = Path(path).stat() if path and slots.get(use.slot, {}).get('enabled', True) else None
            files.append((use.resource_id, path, stat.st_size if stat else None,
                          stat.st_mtime_ns if stat else None))
        from .browser_material import prepare_material, RENDERER_VERSION
        key = (RENDERER_VERSION, definition, tuple(files))
        with lock:
            cached = item.setdefault('_prepared_materials', {}).get(wid)
        spec = cached[1] if cached is not None and cached[0] == key else prepare_material(definition, item['paths'], root)
        prepared[wid] = spec
        with lock:
            item['_prepared_materials'][wid] = (key, spec)
    with lock:
        for wid in tuple(item.get('_prepared_materials', {})):
            if wid not in resolved:
                item['_prepared_materials'].pop(wid, None)
    payload = dict(model_path=str(item['path']), catalog=catalog_to_payload(item['catalog']),
        materials=prepared,
        assignments={a.binding_id: a.working_id for a in state.assignments},
        assimp_dll=str(assimp_dll), target=asdict(state.target))
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    destination = Path(root) / (hashlib.sha256(blob).hexdigest() + '.render.json')
    _publish_cache_file(destination,lambda staged:staged.write_bytes(blob))
    return destination
