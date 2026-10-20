"""Material metadata; originals and renderers are attached in later steps.

Records belong to the project, not to canvas graphics items or workspace pages.
The existing SQLite project writer persists these dictionaries.
"""
import copy
import json
import uuid
from datetime import datetime, timezone

from PyQt6 import QtCore


def iter_visible_materials(scene):
    """Metadata only, shared by tags/search; honor deleted parent folders."""
    nodes = {node['id']: node for node in getattr(scene, 'workspace_nodes', []) or []}
    for material in getattr(scene, 'material_records', []) or []:
        if material.get('deletedAt') is not None:
            continue
        folder, seen, visible = material.get('folderId'), set(), True
        while folder in nodes:
            if folder in seen or nodes[folder].get('deletedAt') is not None:
                visible = False
                break
            seen.add(folder)
            folder = nodes[folder].get('parentId')
        if visible:
            yield material


def validate_material_records(records, project_id):
    if not isinstance(records, list):
        raise ValueError('材质库记录必须是列表')
    seen = set()
    for record in records:
        if (not isinstance(record, dict)
                or not isinstance(record.get('id'), str) or not record['id']
                or record['id'] in seen
                or record.get('projectId') != project_id
                or record.get('schemaVersion') != 1
                or not isinstance(record.get('name'), str)
                or not record['name'].strip() or len(record['name']) > 160
                or not isinstance(record.get('notes'), str)
                or (record.get('folderId') is not None
                    and not isinstance(record['folderId'], str))
                or not isinstance(record.get('tags'), list)
                or not all(isinstance(t, str) for t in record['tags'])
                or not isinstance(record.get('textures'), dict)
                or not isinstance(record.get('preview'), dict)
                or not isinstance(record.get('nativePackages'), list)
                or record.get('status') not in ('draft', 'ready')
                or not isinstance(record.get('createdAt'), str)
                or not isinstance(record.get('updatedAt'), str)
                or (record.get('deletedAt') is not None
                    and not isinstance(record['deletedAt'], str))):
            raise ValueError('材质库记录无效，已停止处理以避免丢失资料')
        json.dumps(record, allow_nan=False)
        if 'files' in record:
            import re
            files = record['files']
            if not isinstance(files, list):
                raise ValueError('原始贴图清单无效')
            for file in files:
                if (not isinstance(file, dict) or not isinstance(file.get('name'), str)
                        or not file['name'] or any(c in file['name'] for c in '\\/:\x00')
                        or not isinstance(file.get('attachmentId'), int) or file['attachmentId'] < 1
                        or not isinstance(file.get('sha256'), str)
                        or not re.fullmatch('[0-9a-f]{64}', file['sha256'])
                        or not isinstance(file.get('size'), int) or file['size'] < 1
                        or not isinstance(file.get('width'), int) or file['width'] < 1
                        or not isinstance(file.get('height'), int) or file['height'] < 1):
                    raise ValueError('原始贴图记录无效')
            for binding in record['textures'].values():
                if not isinstance(binding, dict) or 'attachmentId' not in binding:
                    continue
                if (binding.get('channel') not in ('RGB', 'R', 'G', 'B', 'A')
                        or not any(f['attachmentId'] == binding['attachmentId']
                                   and f['sha256'] == binding.get('sha256') for f in files)):
                    raise ValueError('贴图槽位引用无效')
        seen.add(record['id'])


class MaterialLibraryService(QtCore.QObject):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, scene, folders, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.folders = folders
        self.project_id = scene.project_id
        records = copy.deepcopy(getattr(scene, 'material_records', []))
        validate_material_records(records, self.project_id)
        self.records = records
        scene.material_records = self.records

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec='microseconds')

    def get(self, record_id):
        record = next((r for r in self.records if r['id'] == record_id), None)
        return copy.deepcopy(record)

    def _validate_folder(self, folder_id):
        if folder_id is None:
            return
        node = self.folders._by_id(folder_id)
        if (not node or node['section'] != 'material'
                or node['nodeType'] != 'folder' or not self._folder_visible(folder_id)):
            raise ValueError('请选择有效的材质文件夹')

    def _folder_visible(self, folder_id):
        # A purged folder must not strand its remaining metadata forever.
        node = self.folders._by_id(folder_id)
        while node:
            if node.get('deletedAt') is not None:
                return False
            node = self.folders._by_id(node.get('parentId'))
        return True

    def list_records(self, folder_id=None, query=''):
        query = query.casefold().strip()
        scope = ({folder_id} | set(self.folders._descendants(folder_id))
                 if folder_id is not None else None)
        return [copy.deepcopy(r) for r in self.records
                if r.get('deletedAt') is None
                and self._folder_visible(r.get('folderId'))
                and (scope is None or r.get('folderId') in scope)
                and (not query or query in (
                    r['name'] + '\n' + r['notes'] + '\n' + '\n'.join(r['tags'])).casefold())]

    def create(self, folder_id=None, name=None):
        self._validate_folder(folder_id)
        if name is None:
            name = '未命名材质'
            taken = {r['name'] for r in self.list_records()}
            number = 2
            while name in taken:
                name = f'未命名材质 {number}'
                number += 1
        name = self._name(name)
        now = self._now()
        record = dict(id=uuid.uuid4().hex, projectId=self.project_id,
                      schemaVersion=1, name=name, notes='', folderId=folder_id,
                      tags=[], textures={}, preview={}, nativePackages=[],
                      status='draft', createdAt=now, updatedAt=now, deletedAt=None)
        self.records.append(record)
        self.changed.emit({'id': record['id'], 'operation': 'create'})
        return copy.deepcopy(record)

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
            raise ValueError('材质名称不能为空，最多160个字')
        return name.strip()

    def update(self, record_id, *, name, notes):
        name = self._name(name)
        if not isinstance(notes, str):
            raise ValueError('材质说明必须是文字')
        record = next((r for r in self.records if r['id'] == record_id), None)
        if record is None or record.get('deletedAt') is not None:
            raise ValueError('材质不存在')
        if (record['name'], record['notes']) != (name, notes):
            record.update(name=name, notes=notes, updatedAt=self._now())
            self.changed.emit({'id': record_id, 'operation': 'update'})
        return copy.deepcopy(record)

    def configure(self, record_id, *, slot=None, prepared=None, channel=None,
                  blend_mode=None, tags=None, normal_convention=None,
                  slot_settings=None, properties_patch=None, reset_adjustments=False):
        """Commit a slot and its original together; filenames never decide meaning."""
        from prism.material_textures import SLOTS, CHANNELS
        record = next((r for r in self.records if r['id'] == record_id), None)
        if record is None or record.get('deletedAt') is not None:
            raise ValueError('材质不存在')
        candidate = copy.deepcopy(record)
        attachment = None
        if slot is not None:
            if slot not in SLOTS or (channel is not None and channel not in CHANNELS):
                raise ValueError('无效的槽位或通道')
            if prepared is not None:
                import hashlib
                info, blob = prepared
                if hashlib.sha256(blob).hexdigest() != info['sha256']:
                    raise ValueError('原图校验失败')
                files = candidate.setdefault('files', [])
                file = next((f for f in files if f['sha256'] == info['sha256']
                             and f['name'] == info['name']), None)
                if file is None:
                    # Reuse an original already owned by another material.
                    file = next((f for r in self.records for f in r.get('files', [])
                                 if f['sha256'] == info['sha256']
                                 and f['attachmentId'] in self.scene.attachments), None)
                    if file is None:
                        file = dict(info, attachmentId=max(self.scene.attachments.keys(), default=0) + 1)
                        attachment = (file['attachmentId'], (file['name'], file['mime'], blob))
                    else:
                        file = dict(info, attachmentId=file['attachmentId'])
                    files.append(copy.deepcopy(file))
                candidate['textures'][slot] = dict(file, channel=channel or 'RGB',
                    colorSpace='sRGB' if slot in ('basecolor', 'emissive') else 'linear')
            elif channel is not None:
                if slot not in candidate['textures']:
                    raise ValueError('请先给槽位添加图片')
                candidate['textures'][slot]['channel'] = channel
            else:
                candidate['textures'].pop(slot, None)
        props = candidate.setdefault('preview', {}).setdefault('properties', {})
        if reset_adjustments:
            # Texture/channel assignments and source conventions are durable;
            # inspector slider/tint/flip adjustments are temporary previews.
            props.pop('slots', None)
            for key in ('flipX', 'flipY', 'alphaCutoff'):
                props.pop(key, None)
            if candidate != record:
                candidate['preview']['discardedAdjustments'] = True
        if blend_mode is not None:
            if blend_mode not in ('auto', 'opaque', 'masked', 'translucent'):
                raise ValueError('无效的透明方式')
            props['blendMode'] = blend_mode
        if normal_convention is not None:
            if normal_convention not in ('DirectX', 'OpenGL'):
                raise ValueError('无效的法线格式')
            props['normalConvention'] = normal_convention
        if slot_settings is not None:
            from prism.material_parameters import validate_slot_patch
            setting_slot, patch = slot_settings
            checked = validate_slot_patch(setting_slot, patch)
            props.setdefault('slots', {}).setdefault(setting_slot, {}).update(checked)
        if properties_patch is not None:
            for key, value in properties_patch.items():
                if key in ('flipX', 'flipY', 'ignoreBaseAlpha') and isinstance(value, bool):
                    props[key] = value
                elif key == 'alphaCutoff' and isinstance(value, (int, float)) and 0 <= value <= 1:
                    props[key] = value
                else:
                    raise ValueError('无效的材质设置')
        if tags is not None:
            if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                raise ValueError('标签必须是文字列表')
            candidate['tags'] = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
        if candidate == record:
            return copy.deepcopy(record)
        candidate['updatedAt'] = self._now()
        from prism.material_textures import render_signature
        if render_signature(candidate) != render_signature(record):
            candidate['preview']['stale'] = True
        validate_material_records([candidate], self.project_id)
        if attachment:
            self.scene.attachments[attachment[0]] = attachment[1]
        tags_changed = candidate.get('tags') != record.get('tags')
        files_changed = candidate.get('files') != record.get('files')
        record.clear()
        record.update(candidate)
        self.changed.emit({'id': record_id, 'operation': 'configure',
                           'tagsChanged': tags_changed, 'filesChanged': files_changed})
        return copy.deepcopy(record)

    def save_preview(self, record_id, signature, blob):
        from prism.material_textures import render_signature
        record = next((r for r in self.records if r['id'] == record_id), None)
        if record is None or render_signature(record) != signature:
            return False  # A render finishing after a slot edit cannot overwrite it.
        preview = record.setdefault('preview', {})
        attachment_id = preview.get('thumbnailAttachmentId')
        original_ids = {f['attachmentId'] for r in self.records for f in r.get('files', [])}
        if not isinstance(attachment_id, int) or attachment_id in original_ids:
            attachment_id = max(self.scene.attachments.keys(), default=0) + 1
        self.scene.attachments[attachment_id] = ('material-preview.png', 'image/png', blob)
        preview.update(thumbnailAttachmentId=attachment_id, signature=signature, stale=False)
        preview.pop('discardedAdjustments', None)
        record['status'] = 'ready'
        record['updatedAt'] = self._now()
        self.changed.emit({'id': record_id, 'operation': 'preview'})
        return True
