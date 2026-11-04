"""Material metadata; originals and renderers are attached in later steps.

Records belong to the project, not to canvas graphics items or workspace pages.
The existing SQLite project writer persists these dictionaries.
"""
import copy
import json
import math
import uuid
from datetime import datetime, timezone

from PyQt6 import QtCore
from prism.library_observability import observed, event as library_event


def material_validation_fingerprint(records, project_id):
    """Content proof for loader-validated, project-owned JSON metadata."""
    import hashlib
    payload = json.dumps((project_id, records), sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).digest()


class _OwnershipProof:
    """Proof that one record list is validated and owned by one service.

    ``records`` is the list object itself: replacing the list on the scene
    (a new project, a fresh read) breaks the identity check, and
    ``revision`` is bumped by every mutation the owning service commits, so
    an edit invalidates the proof instead of letting a stale validation be
    reused as if nothing had changed.  A content proof left on the scene is
    checked first, because it can tell that the list changed behind the
    owner's back.
    """

    __slots__ = ('records', 'revision', 'project_id')

    def __init__(self, records, revision, project_id):
        self.records = records
        self.revision = revision
        self.project_id = project_id

    def matches(self, records, revision, project_id):
        return (self.records is records and self.revision == revision
                and self.project_id == project_id)


def material_ownership_proof(scene):
    """The scene's current ownership proof, or ``None``."""
    return getattr(scene, '_material_owner_proof', None)


def rebind_material_records(scene, records, *, project_id=None, revision=0):
    """Adopt an already validated record list without copying or revalidating.

    This is the hand-off point for a resource tree that installs its own
    material model: it publishes an ownership proof so the next
    ``MaterialLibraryService`` can reuse the list instead of copying and
    validating every record again.

    The caller is responsible for the validation -- this function checks
    nothing about the records.  It returns ``True`` when the list was
    adopted.
    """
    if records is None:
        return False
    project_id = project_id or getattr(scene, 'project_id', None)
    if project_id is None:
        return False
    scene._material_owner_proof = _OwnershipProof(records, revision, project_id)
    scene._material_content_revision = revision
    scene._material_validation_proof = None
    scene.material_records = records
    return True


def iter_visible_materials(scene):
    """Metadata only, shared by tags/search; honor deleted parent folders."""
    yield from iter_visible_library_records(scene, getattr(scene, 'material_records', ()),
                                           folder_field='folderId', deleted_field='deletedAt')


def iter_visible_library_records(scene, records, *, folder_field, deleted_field):
    """Shared metadata visibility for library search and stale-result checks."""
    nodes = {node['id']: node for node in getattr(scene, 'workspace_nodes', []) or []}
    for material in records or ():
        if material.get(deleted_field) not in (None, '', 0):
            continue
        folder, seen, visible = material.get(folder_field), set(), True
        while folder in nodes:
            if folder in seen or nodes[folder].get('deletedAt') is not None:
                visible = False
                break
            seen.add(folder)
            folder = nodes[folder].get('parentId')
        if visible:
            yield material


def _assert_json_value(value, depth=0):
    """Raise what ``json.dumps(value, allow_nan=False)`` would raise.

    ``json.dumps`` was called once per record to catch values that are not
    JSON: a non-finite float, or an object of some other type. On a 100 000
    record material library that was 3.1 s of pure serialisation on *every*
    tree rebuild -- the trace showed it as a 3,137 ms stall in
    ``material_service.validate_material_records``.

    Both failures can be detected by a walk that allocates nothing, so the
    guarantee is kept and the cost is not.

    The walk follows ``json`` exactly: a tuple is a JSON array and an
    ``int``/``float``/``bool``/``None`` key is written as its string form,
    so those are accepted here too. Being *stricter* than ``json.dumps``
    would reject records the project is entitled to store.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Out of range float values are not JSON compliant')
        return
    if depth > 200:
        # `json.dumps` reports a circular structure as a ValueError; a plain
        # recursion would raise RecursionError instead, which reads like a
        # crash rather than bad data.
        raise ValueError('Circular reference detected')
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, (str, int, float, bool)) and key is not None:
                raise TypeError('keys must be str, int, float, bool or None, '
                                'not %s' % type(key).__name__)
            _assert_json_value(item, depth + 1)
        return
    raise TypeError('Object of type %s is not JSON serializable'
                    % type(value).__name__)


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
        _assert_json_value(record)
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


def _record_ids(record_ids):
    """把入参整理成 id 列表；非法的入参在这里就报错。

    未知 id 不在这里报 —— 那是数据状态，不是入参错误：
    调用方传的 id 里混进一个已经不存在的，不该让整批操作失败。
    """
    if record_ids is None or isinstance(record_ids, (str, bytes)):
        raise ValueError('材质 id 列表无效')
    try:
        ids = list(record_ids)
    except TypeError:
        raise ValueError('材质 id 列表无效') from None
    if not all(isinstance(record_id, str) and record_id for record_id in ids):
        raise ValueError('材质 id 必须是文字')
    return ids


def _change(operation, record_ids):
    """回收站操作的通知载荷。

    'id' 给一次只看一条的界面，'ids' 给一次看一批的回收站 ——
    两个都带上，界面不用猜这次动了哪些材质。空操作也照发信号：
    界面按信号刷新，调用方不必猜这次调用到底有没有生效。
    """
    ids = sorted(record_ids)
    return {'id': ids[0] if len(ids) == 1 else None,
            'ids': ids, 'operation': operation}


class MaterialLibraryService(QtCore.QObject):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, scene, folders, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.folders = folders
        self.project_id = scene.project_id
        current = getattr(scene, 'material_records', [])
        revision = getattr(scene, '_material_content_revision', 0)
        owner = getattr(scene, '_material_owner_proof', None)
        proof = getattr(scene, '_material_validation_proof', None)
        if proof is not None:
            # A content proof says something about the content itself, so it
            # is checked before the ownership proof.  The ownership proof only
            # knows that the same list object is still there; a list that was
            # changed behind the owner's back has to be validated again
            # instead of being reused as if nothing had changed.
            if proof == material_validation_fingerprint(current, self.project_id):
                # The reader already validated this owned list off the GUI thread.
                # Recheck content (not just identity) before transferring ownership.
                records = current
            else:
                records = copy.deepcopy(current)
                validate_material_records(records, self.project_id)
        elif owner is not None and owner.matches(current, revision, self.project_id):
            # Same list, same content revision: it was validated already and
            # nothing has changed since. Reuse it as is -- no copy, no
            # revalidation, and no fingerprint of every record.
            records = current
        else:
            records = copy.deepcopy(current)
            validate_material_records(records, self.project_id)
        scene._material_validation_proof = None
        self.records = records
        scene.material_records = self.records
        self._content_revision = revision
        scene._material_content_revision = revision
        scene._material_owner_proof = _OwnershipProof(
            records, revision, self.project_id)
        self.revision = 0
        self.changed.connect(self._advance_revision)

    def reuse_validated_records(self):
        """The owned records when the ownership proof still holds, else None.

        A resource tree can ask for the already validated list instead of
        copying and validating every record again.  The answer is None as
        soon as the scene holds a different list: another owner took over
        and this service's records are no longer the project's.
        """
        owner = getattr(self.scene, '_material_owner_proof', None)
        revision = getattr(self.scene, '_material_content_revision', 0)
        if (owner is not None and owner.matches(self.records, revision,
                                                self.project_id)
                and getattr(self.scene, 'material_records', None) is self.records):
            return self.records
        return None

    def _advance_revision(self, _change):
        self.revision += 1
        # Every mutation the service commits keeps the list valid: a changed
        # record is validated before it replaces the old one, and a created
        # record is built to the contract. The proof is therefore refreshed
        # with a new revision instead of being dropped. It still fails for
        # any other owner, and a list that is replaced on the scene fails the
        # identity check.
        self._content_revision += 1
        self.scene._material_content_revision = self._content_revision
        self.scene._material_owner_proof = _OwnershipProof(
            self.records, self._content_revision, self.project_id)
        self.scene._material_validation_proof = None

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
        seen = set()
        while node:
            if node['id'] in seen or node.get('deletedAt') is not None:
                return False
            seen.add(node['id'])
            node = self.folders._by_id(node.get('parentId'))
        return True

    def _matching_records(self, folder_id=None, query='', record_ids=None):
        """Read owned metadata once; callers choose an isolated projection."""
        return (record for record in self._matching_scan(folder_id, query, record_ids)
                if record is not None)

    def _matching_scan(self, folder_id=None, query='', record_ids=None):
        """One step per examined row, including rows rejected by the filter."""
        query = query.casefold().strip()
        scope = ({folder_id} | set(self.folders._descendants(folder_id))
                 if folder_id is not None else None)
        visible = {}
        for record in self.records:
            if record_ids is not None and record['id'] not in record_ids:
                yield None
                continue
            folder = record.get('folderId')
            if record.get('deletedAt') is not None or (scope is not None and folder not in scope):
                yield None
                continue
            if folder not in visible:
                visible[folder] = self._folder_visible(folder)
            if visible[folder] and (not query or query in (
                    record['name'] + '\n' + record['notes'] + '\n' + '\n'.join(record['tags'])).casefold()):
                yield record
            else:
                yield None

    def list_records(self, folder_id=None, query='', *, record_ids=None):
        ids = set(record_ids) if record_ids is not None else None
        return [copy.deepcopy(r) for r in self._matching_records(folder_id, query, ids)]

    def iter_record_batches(self, folder_id=None, query='', *, chunk_size=64):
        """Yield visible records in bounded batches.

        Same selection as ``list_records``, but a 5000 material library
        never becomes one 5000 element list: a batch queue can be fed chunk
        by chunk and only keeps one chunk of records alive.
        """
        size = max(1, int(chunk_size))
        batch = []
        for record in self._matching_records(folder_id, query):
            batch.append(copy.deepcopy(record))
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    def list_card_records(self, folder_id=None, query=''):
        """Detached display metadata; never copy original-file manifests or parameters.

        These summaries are for cards only. Editing, rendering and export must
        fetch the complete record through get/list_records.
        """
        return [record for batch in self.iter_card_record_batches(folder_id, query)
                for record in batch]

    def iter_card_record_batches(self, folder_id=None, query='', *, chunk_size=64):
        """Detached summaries, bounded by rows scanned rather than hits found.

        A zero-hit search must yield too. Consumers may abandon this iterator
        between batches when its service revision or query has changed.
        """
        size = max(1, int(chunk_size))
        result = []
        for scanned, record in enumerate(self._matching_scan(folder_id, query), 1):
            if record is not None:
                result.append(self._card_record(record))
            if scanned % size == 0:
                yield result
                result = []
        if result:
            yield result

    @staticmethod
    def _card_record(record):
        textures = record.get('textures', {})
        return dict(
            id=record['id'], name=record['name'], tags=list(record['tags']),
            textures={slot: {key: binding.get(key, 0) for key in ('width', 'height')}
                      if isinstance(binding, dict) else None
                      for slot, binding in textures.items()},
            preview={key: record.get('preview', {}).get(key) for key in (
                'signature', 'thumbnailAttachmentId', 'discardedAdjustments')},
            nativePackages=[{'engineVersion': package.get('engineVersion')}
                            if isinstance(package, dict) else None
                            for package in record.get('nativePackages', [])],
            fileCount=len(record.get('files', textures)))

    @observed('material','create')
    def create(self, folder_id=None, name=None):
        self._validate_folder(folder_id)
        if name is None:
            name = '未命名材质'
            taken = {r['name'] for r in self._matching_records()}
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

    @observed('material','metadata')
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

    @observed('material','move')
    def move_records(self, record_ids, folder_id):
        self._validate_folder(folder_id)
        ids = list(dict.fromkeys(record_ids))
        index = {r['id']: r for r in self.records}
        if any(i not in index or index[i].get('deletedAt') is not None for i in ids):
            raise ValueError('选中的材质已不存在')
        changed = [index[i] for i in ids if index[i].get('folderId') != folder_id]
        stamp = self._now()
        for record in changed:
            record.update(folderId=folder_id, updatedAt=stamp)
        if changed:
            self.changed.emit({'ids': [r['id'] for r in changed], 'operation': 'move'})
        return len(changed)

    @observed('material','binding')
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
            from prism.material_parameters import validate_texture_channel, texture_color_space
            if prepared is not None or channel is not None:
                validate_texture_channel(slot, channel or 'RGB')
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
                    colorSpace='sRGB' if texture_color_space(slot) == 'srgb' else 'linear')
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
        binding=candidate.get('textures',{}).get(slot,{})
        library_event('material','binding_state','installed',project_id=self.project_id,
            material_id=record_id,slot=slot,channel=binding.get('channel'),
            attachment_id=binding.get('attachmentId'),sha256=binding.get('sha256'),
            width=binding.get('width'),height=binding.get('height'),color_space=binding.get('colorSpace'),
            durable=False,scope='scene_memory')
        self.changed.emit({'id': record_id, 'operation': 'configure',
                           'tagsChanged': tags_changed, 'filesChanged': files_changed})
        return copy.deepcopy(record)

    @observed('material','cover_install')
    def save_preview(self, record_id, signature, blob):
        from prism.material_textures import render_signature
        record = next((r for r in self.records if r['id'] == record_id), None)
        if record is None or render_signature(record) != signature:
            library_event('material','cover_install','stale',material_id=record_id,
                          signature=signature,reason='record_missing_or_signature_changed')
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

    # ── 回收站 ──────────────────────────────────────────────────────
    # 材质记录本来就带 deletedAt 字段，所以软删只是改一个字段：
    # 原始贴图和预览都不用重写，误删的材质还能原样回来。

    @observed('material', 'trash')
    def trash(self, record_ids, deleted_at=None):
        """软删：给记录打上删除时间，让它离开可见的材质列表。

        重复删除不会改掉第一次的时间 —— 回收站按删除时间排序，
        第二次调用把时间刷成现在，只会让「什么时候删的」变得不准。
        """
        ids = _record_ids(record_ids)
        if deleted_at is None:
            deleted_at = self._now()
        elif not isinstance(deleted_at, str) or not deleted_at.strip():
            raise ValueError('删除时间无效')
        index = {r['id']: r for r in self.records}
        marked = set()
        for record_id in ids:
            record = index.get(record_id)
            if record is None or record.get('deletedAt') is not None:
                continue
            record['deletedAt'] = deleted_at
            marked.add(record_id)
        self.changed.emit(_change('trash', marked))
        return marked

    @observed('material', 'restore')
    def restore(self, record_ids):
        """从回收站恢复：把 deletedAt 清回 None。

        没被软删的记录原样跳过 —— 恢复不是「确保删除状态」，
        顺手碰一下没删过的材质只会让调用方分不清自己到底改了什么。
        """
        ids = _record_ids(record_ids)
        index = {r['id']: r for r in self.records}
        restored = set()
        for record_id in ids:
            record = index.get(record_id)
            if record is None or record.get('deletedAt') is None:
                continue
            record['deletedAt'] = None
            restored.add(record_id)
        self.changed.emit(_change('restore', restored))
        return restored

    @observed('material', 'purge')
    def purge(self, record_ids):
        """永久删除：把这些记录从 records 里真正拿掉。

        回收站入口由界面把守（§7.6「永久删除仅在回收站执行」）；
        服务层按传入的 id 移除，界面口径不一致时不该炸成异常。
        """
        ids = _record_ids(record_ids)
        index = {r['id']: r for r in self.records}
        removed = {record_id for record_id in ids if record_id in index}
        if removed:
            # 原地改列表：scene.material_records 和别处拿到的引用都指着它。
            self.records[:] = [r for r in self.records if r['id'] not in removed]
        self.changed.emit(_change('purge', removed))
        return removed
