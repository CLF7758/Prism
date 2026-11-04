"""Bounded, per-project recovery records. No live Qt objects or project writes.

Pending records survive cleanup. Only complete newer checkpoints of the same
project supersede old ones; resolved history is disposable. The GUI captures
plain data and the existing IO worker calls this module.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import uuid
import base64
import threading
from functools import wraps

logger = logging.getLogger(__name__)
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
HISTORY_PER_PROJECT = 3
RESOLVED_DAYS = 30


def _encode(value):
    if isinstance(value, bytes):
        return {'__prism_recovery_bytes__': base64.b64encode(value).decode('ascii')}
    raise TypeError(type(value).__name__)


def _decode(value):
    if set(value) == {'__prism_recovery_bytes__'}:
        return base64.b64decode(value['__prism_recovery_bytes__'], validate=True)
    return value


def _locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


def project_key(project_id, filename):
    source = os.path.normcase(os.path.abspath(filename)) if filename else 'untitled'
    return hashlib.sha256(f'{project_id}\0{source}'.encode('utf-8')).hexdigest()


class RecoveryStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()

    @_locked
    def records(self):
        if not self.root.exists():
            return []
        records = []
        for path in self.root.glob('*/*.json'):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            try:
                if path.stat().st_size > MAX_RECORD_BYTES:
                    continue
                value = json.loads(path.read_text(encoding='utf-8'), object_hook=_decode)
                if (value.get('version') != 1 or value.get('key') != path.parent.name
                        or not isinstance(value.get('payload'), dict)
                        or not isinstance(value.get('created'), (float, int))
                        or not isinstance(value.get('session'), str)
                        or value.get('status') not in ('pending', 'saved', 'discarded')):
                    continue
                value['_path'] = str(path)
                records.append(value)
            except (OSError, ValueError, TypeError, AttributeError):
                logger.warning('Unreadable recovery record: %s', path.name)
        return sorted(records, key=lambda value: value['created'], reverse=True)

    @_locked
    def write(self, payload, *, session, status='pending', owner_pid=None):
        key = project_key(payload.get('project_id'), payload.get('filename'))
        value = dict(version=1, key=key, session=session,
                     pid=os.getpid() if owner_pid is None else owner_pid, status=status,
                     created=time.time(), payload=payload)
        encoded = json.dumps(value, ensure_ascii=False, default=_encode).encode('utf-8')
        if len(encoded) > MAX_RECORD_BYTES:
            raise OSError('恢复记录超过 64 MiB 上限，请立即保存工程')
        self.cleanup()
        total = sum(p.stat().st_size for p in self.root.glob('*/*')
                    if p.is_file() and p.suffix in ('.json', '.tmp'))
        for old in reversed(self.records()):
            if total + len(encoded) <= MAX_TOTAL_BYTES:
                break
            if old['status'] != 'pending':
                path = Path(old['_path'])
                total -= path.stat().st_size
                path.unlink()
        if total + len(encoded) > MAX_TOTAL_BYTES:
            logger.warning('Recovery record capacity reached; pending evidence retained (bytes=%s)', total)
            raise OSError('恢复记录达到 256 MiB 上限；未恢复记录已保留，请保存工程并处理恢复记录')
        folder = self.root / key
        folder.mkdir(parents=True, exist_ok=True)
        if folder.is_symlink():
            raise OSError('Invalid recovery directory')
        path = folder / (uuid.uuid4().hex + '.json')
        self._atomic(path, encoded)
        # Publication is durable before an older complete checkpoint is retired.
        self.cleanup()
        return str(path)

    @staticmethod
    def _atomic(path, encoded):
        temporary = path.with_suffix('.tmp')
        try:
            with temporary.open('xb') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @_locked
    def resolve(self, key, *, through=None, session=None):
        for value in self.records():
            if (value['key'] != key or (through is not None and value['created'] > through)
                    or (session is not None and value['session'] != session)):
                continue
            path = Path(value.pop('_path'))
            value['status'] = 'saved'
            self._atomic(path, json.dumps(value, ensure_ascii=False, default=_encode).encode('utf-8'))
        self.cleanup()

    @_locked
    def finish_session(self, session):
        for value in self.records():
            if (value['session'] != session or value['status'] != 'pending'
                    or value['payload'].get('full_project')):
                continue
            path = Path(value.pop('_path'))
            value['status'] = 'discarded'
            self._atomic(path, json.dumps(value, ensure_ascii=False, default=_encode).encode('utf-8'))
        self.cleanup()

    @_locked
    def cleanup(self, *, now=None):
        now = time.time() if now is None else now
        # Interrupted atomic writes are not published recovery records. Count
        # them against capacity and remove only abandoned, day-old temporaries.
        for path in self.root.glob('*/*.tmp'):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            try:
                if now-path.stat().st_mtime > 86400:
                    path.unlink()
            except OSError:
                logger.warning('Recovery temporary is still busy: %s', path.name)
        groups = {}
        for value in self.records():
            group_key = (value['key'], value['session'] if value['status'] == 'pending' else 'saved')
            group = groups.setdefault(group_key, [])
            resolved_expired = (value['status'] != 'pending' and
                                now-value['created'] > RESOLVED_DAYS*86400)
            if resolved_expired or len(group) >= HISTORY_PER_PROJECT:
                # Only unlink records enumerated under our own validated root.
                Path(value['_path']).unlink(missing_ok=True)
            else:
                group.append(value)

    @_locked
    def pending(self, session):
        seen, result = set(), []
        for value in self.records():
            if (value['status'] != 'pending' or value['session'] == session
                    or value.get('pid') == os.getpid() or value['key'] in seen):
                continue
            seen.add(value['key'])
            result.append(value)
        return result


def recover_copy(record, destination, *, cancel=None):
    """Reuse Save As copying/conversion, patching only the private destination."""
    import sqlite3
    from contextlib import closing
    from prism.storage.managed import root_for
    from prism.storage.managed_io import _copy_directory
    from prism.storage.migration import convert_legacy
    from prism.partial_project import page_row
    payload = record['payload']
    source = payload.get('filename')
    if not source or not Path(source).exists():
        raise FileNotFoundError('恢复所需的原工程不存在，请先找回或重新连接原工程')

    def prepare(metadata):
        with closing(sqlite3.connect(metadata)) as db:
            identity = db.execute("SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()
            if not identity or identity[0] != payload.get('project_id'):
                raise ValueError('恢复记录与原工程身份不一致，未执行恢复')
            if payload.get('full_project'):
                return
            with db:
                # Unloaded page bodies remain in the copied database. A missing
                # content key must never replace them with null.
                for order, page in enumerate(payload.get('workspace_pages', ())):
                    if page.get('kind') not in ('document', 'mindmap') or 'content' not in page:
                        continue
                    row = db.execute('SELECT data FROM workspace_pages WHERE id=?', (page['id'],)).fetchone()
                    data = json.loads(row[0]) if row else page_row(page, order)[2]
                    data['content'] = page['content']
                    db.execute('INSERT INTO workspace_pages(id,kind,title,data,sort_order) VALUES(?,?,?,?,?) '
                               'ON CONFLICT(id) DO UPDATE SET title=excluded.title,data=excluded.data',
                               (page['id'], page['kind'], page.get('title', ''), json.dumps(data, ensure_ascii=False), order))
                for node in payload.get('workspace_nodes', ()):
                    db.execute('INSERT INTO workspace_nodes(id,payload) VALUES(?,?) '
                               'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                               (node['id'], json.dumps(node, ensure_ascii=False)))
                for identifier, name, mime, blob in payload.get('attachments', ()):
                    # Original attachments stay untouched unless explicitly
                    # captured as dirty/resident editor attachments.
                    db.execute('INSERT INTO attachments(id,name,mime,blob) VALUES(?,?,?,?) '
                               'ON CONFLICT(id) DO UPDATE SET name=excluded.name,mime=excluded.mime,blob=excluded.blob',
                               (identifier, name, mime, blob))
                if payload.get('attachments'):
                    from prism.storage.managed import _externalize
                    from prism.storage.dos_repository import DosObjectRepository
                    repository = DosObjectRepository(Path(metadata).parent/'objects')
                    try:
                        _externalize(db, repository, 'attachments',
                                     [row[0] for row in payload['attachments']], cancel)
                    finally:
                        repository.close()
                db.execute('UPDATE board_notes SET html=? WHERE board_id=0', (payload.get('note_html', ''),))
                db.execute('UPDATE board_mindmap SET tree=? WHERE board_id=0',
                           (json.dumps(payload.get('mindmap_tree'), ensure_ascii=False),))

    folder = root_for(source)
    if folder is not None:
        _copy_directory(folder, Path(destination).resolve(), lambda _: None, cancel)
    else:
        convert_legacy(source, destination, experimental=True, managed=True,
                       cancel=cancel)
    # Publish the source copy before opening new object-store attachment
    # handles: Windows can otherwise refuse to rename the staging directory.
    # The UI only offers/open this destination after the entire recovery ends.
    try:
        prepare(Path(destination)/'metadata.sqlite')
    except Exception:
        (Path(destination)/'.recovery-failed').write_text('Recovery incomplete; original source untouched.', encoding='utf-8')
        raise
    return str(Path(destination)/'metadata.sqlite')
