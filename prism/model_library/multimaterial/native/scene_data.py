"""Source-addressed geometry for the isolated renderer; never imported by GUI.

glTF/OBJ use source structural keys, not material names or render order. FBX
uses the pinned Assimp reader and refuses ambiguous source/native joins.
"""
import json
import re
from pathlib import Path

import numpy as np


def read_parts(path, catalog, assimp_dll):
    path = Path(path)
    if path.suffix.lower() in ('.gltf', '.glb'):
        return _gltf_parts(path, catalog)
    if path.suffix.lower() == '.obj':
        return _obj_parts(path, catalog)
    return _fbx_parts(path, catalog, assimp_dll)


def _gltf_parts(path, catalog):
    from prism.model_library.preview_channels import read_gltf, _uri_bytes
    doc, binary = read_gltf(path)
    buffers = [_uri_bytes(b['uri'], path.parent) if 'uri' in b else binary
               for b in doc.get('buffers', [])]

    def accessor(index):
        a = doc['accessors'][index]
        if 'sparse' in a or 'bufferView' not in a:
            raise ValueError('该模型使用的稀疏几何访问器尚未接入编辑预览')
        view = doc['bufferViews'][a['bufferView']]
        dtype = np.dtype({5120:'i1',5121:'u1',5122:'<i2',5123:'<u2',5125:'<u4',5126:'<f4'}[a['componentType']])
        width = {'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT4':16}[a['type']]
        stride = view.get('byteStride', width*dtype.itemsize)
        start = view.get('byteOffset', 0)+a.get('byteOffset', 0)
        array = np.ndarray((a['count'],width),dtype=dtype,buffer=buffers[view['buffer']],
                           offset=start,strides=(stride,dtype.itemsize)).copy()
        if a.get('normalized') and dtype.kind in ('u','i'):
            array = np.maximum(array.astype('float32')/np.iinfo(dtype).max,-1)
        return array

    nodes = doc.get('nodes', [])
    parents = {}
    for i, node in enumerate(nodes):
        for child in node.get('children', []):
            if child in parents:
                raise ValueError('节点含多个父级，无法确认实例变换')
            parents[child] = i
    matrices = {}
    def world(i, visiting=()):
        if i in matrices:return matrices[i]
        if i in visiting:raise ValueError('模型节点形成循环')
        n = nodes[i]
        if 'matrix' in n:
            m = np.asarray(n['matrix'],dtype=float).reshape(4,4).T
        else:
            x,y,z,w = n.get('rotation',[0,0,0,1])
            r = np.array([[1-2*y*y-2*z*z,2*x*y-2*z*w,2*x*z+2*y*w],
                          [2*x*y+2*z*w,1-2*x*x-2*z*z,2*y*z-2*x*w],
                          [2*x*z-2*y*w,2*y*z+2*x*w,1-2*x*x-2*y*y]])
            m = np.eye(4);m[:3,:3] = r@np.diag(n.get('scale',[1,1,1]))
            m[:3,3] = n.get('translation',[0,0,0])
        if i in parents:m = world(parents[i],visiting+(i,))@m
        matrices[i] = m
        return m
    result=[]
    for site in catalog.binding_sites:
        node_index = int(re.search(r'\|node:(\d+)',site.binding_id)[1])
        mesh = int(site.geometry_key.rsplit(':',1)[1])
        primitive = int(site.primitive_key.rsplit(':',1)[1])
        n = nodes[node_index]
        if 'skin' in n:raise ValueError('蒙皮模型的局部编辑尚未验证，保留原预览')
        p = doc['meshes'][mesh]['primitives'][primitive]
        attrs=p.get('attributes',{})
        if 'POSITION' not in attrs:raise ValueError('缺少可读取的位置数据')
        vertices=accessor(attrs['POSITION'])
        indices=accessor(p['indices']).reshape(-1).tolist() if 'indices' in p else list(range(len(vertices)))
        mode=p.get('mode',4)
        if mode==4:
            if len(indices)%3:raise ValueError('三角形索引长度无效')
            faces=[indices[i:i+3] for i in range(0,len(indices),3)]
        elif mode==5:
            faces=[([indices[i+1],indices[i],indices[i+2]] if i%2 else indices[i:i+3])
                   for i in range(len(indices)-2)]
        elif mode==6:faces=[[indices[0],indices[i],indices[i+1]] for i in range(1,len(indices)-1)]
        else:raise ValueError('点线图元尚未接入材质编辑')
        result.append(dict(binding_id=site.binding_id,points=vertices,faces=faces,
            normals=accessor(attrs['NORMAL']) if 'NORMAL' in attrs else None,
            uvs={int(k[9:]):accessor(v) for k,v in attrs.items() if k.startswith('TEXCOORD_')},
            matrix=world(node_index)))
    return result


def _obj_parts(path,catalog):
    from prism.model_library.multimaterial.inspectors import obj,text
    raw=path.read_bytes();decoded=text.decode_text(raw)
    positions=[];normals=[];uvs=[];groups={};active=obj._ObjUsage()
    for line in text.lines(raw,decoded):
        key,value=line.keyword,line.raw
        if key=='v':positions.append([float(v) for v in value.split()[:3]])
        elif key=='vn':normals.append([float(v) for v in value.split()[:3]])
        elif key=='vt':uvs.append([float(v) for v in value.split()[:2]])
        elif key=='o':active.object_key=value;active.group_key=b''
        elif key=='g':active.group_key=value
        elif key=='usemtl':active.material_key=value
        elif key=='f':
            bid=obj._usage_id(active)
            target=groups.setdefault(bid,dict(points=[],faces=[],normals=[],uvs={0:[]},matrix=np.eye(4),binding_id=bid,complete_uv=True))
            face=[]
            for word in value.split():
                pieces=word.split(b'/')
                def idx(part,size):
                    index=int(part);return index-1 if index>0 else size+index
                point=idx(pieces[0],len(positions))
                if not 0<=point<len(positions):raise ValueError('OBJ 顶点索引越界')
                face.append(len(target['points']));target['points'].append(positions[point])
                target['uvs'][0].append(uvs[idx(pieces[1],len(uvs))] if len(pieces)>1 and pieces[1] else [0,0])
                if len(pieces)<2 or not pieces[1]:target['complete_uv']=False
                target['normals'].append(normals[idx(pieces[2],len(normals))] if len(pieces)>2 and pieces[2] else [0,0,0])
            target['faces'].append(face)
    result=[]
    for site in catalog.binding_sites:
        if site.binding_id not in groups:raise ValueError('OBJ 使用点与几何不一致')
        part=groups[site.binding_id]
        if not part.pop('complete_uv'):part['uvs']={}
        if not any(any(x for x in n) for n in part['normals']):part['normals']=None
        result.append(part)
    return result


def _fbx_parts(path,catalog,dll):
    from .assimp import AssimpLibrary
    source=AssimpLibrary(dll).read(path)
    definitions={d.material_id:d for d in catalog.definitions}
    names={}
    for d in catalog.definitions:names.setdefault(d.name,[]).append(d.material_id)
    # Two loaders cannot safely infer same-named definitions. Refuse editing
    # rather than adopting one by material array order.
    if any(len(ids)>1 for ids in names.values()):
        raise ValueError('FBX 存在同名不同材质，原生映射尚不能唯一确认')
    candidates=[];stack=[]
    scale=float(source.metadata.get('UnitScaleFactor',1.0))*.01
    for node in source.nodes:
        stack=stack[:node.depth]
        matrix=(stack[-1] if stack else np.eye(4))@np.asarray(node.local_matrix)
        stack.append(matrix)
        for mesh_index in node.mesh_indices:
            mesh=source.meshes[mesh_index]
            if not mesh.points or mesh.bone_count:
                raise ValueError('FBX 几何过大或含未验证的蒙皮数据')
            material=source.materials[mesh.material_index]
            candidates.append((node,mesh,material,matrix.copy()))
    parts=[];used=set()
    counts=json.loads(catalog.unknown_json or '{}').get('face_counts',{})
    for site in catalog.binding_sites:
        definition=definitions.get(site.original_material_id)
        matches=[]
        for i,(node,mesh,material,matrix) in enumerate(candidates):
            same_path=(node.path==site.node_path or node.path.endswith('/'+site.node_path))
            if not same_path:continue
            if definition and material.name!=definition.name:continue
            if site.binding_id in counts and mesh.face_count!=counts[site.binding_id]:continue
            matches.append(i)
        if len(matches)!=1 or matches[0] in used:
            raise ValueError('FBX 部位映射不唯一，拒绝局部编辑：'+site.node_path)
        i=matches[0];used.add(i);node,mesh,material,matrix=candidates[i]
        unit=np.diag([scale,scale,scale,1.])
        parts.append(dict(binding_id=site.binding_id,points=np.asarray(mesh.points).reshape(-1,3),
            faces=mesh.faces,normals=np.asarray(mesh.normals).reshape(-1,3) if mesh.normals else None,
            uvs={0:np.asarray(mesh.uvs).reshape(-1,2)} if mesh.uvs else {},matrix=unit@matrix))
    if len(used)!=len(candidates):raise ValueError('FBX 有未映射的几何，不能宣称完整材质编辑')
    return parts
