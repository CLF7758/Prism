"""A relocatable working copy of a model, protected by a lease.

The native preview host must be able to open a model without knowing the
database and without knowing where the user imported it from.  The bundle
store gives it exactly that: a directory that holds the model and its
dependencies at their package-relative paths, plus a manifest that says what
each file is.

The bundle is a **work copy**, not an export:

* the stored originals are never modified -- the bundle contains copies;
* the bytes that the preview shows are derived resources (for example a
  re-pointed material library) and live in a separate ``_preview`` folder;
* a lease protects the directory while the preview reads it, so a cleanup
  pass can never pull the files away under a running reader;
* the package key contains the source revision, and a derived appearance
  bundle adds the appearance revision, so a stale bundle can never be
  mistaken for the current one.

A reference the original file states as an absolute path cannot be relocated.
The original bytes are still preserved, and the bundle ships a *resolution
map* that says which package-relative file now holds those bytes.  For the
text formats a work copy with rewritten references is produced as well; the
binary formats are never rewritten, because rewriting them would change the
bytes the user owns.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .project_storage import (
    CHUNK_SIZE,
    ModelProjectContext,
    ModelStorageError,
)
from .repository import ModelRepositoryAdapter

#: Where the bundle working copies live.
BUNDLE_ROOT_NAME = 'prism-model-bundles'

#: The manifest of a bundle.
MANIFEST_NAME = 'manifest.json'

#: Folder that holds derived (never original) preview resources.
PREVIEW_DIRECTORY = '_preview'


@dataclass(frozen=True)
class PreparedBundle:
    """A working directory the native host may open."""

    lease_id: str
    package_root: str
    main_relative_path: str
    manifest_path: str
    source_revision: int
    appearance_revision: Optional[int] = None
    model_id: str = ''
    key: str = ''
    manifest: dict = field(default_factory=dict)


class BundleLeaseError(ModelStorageError):
    """The lease is unknown or already released."""


class ModelBundleStore:
    """Prepare and release the working copies the preview host reads."""

    def __init__(self, project_context: ModelProjectContext,
                 repository: Optional[ModelRepositoryAdapter] = None,
                 root: Optional[str] = None):
        self.context = project_context
        self.repository = repository or ModelRepositoryAdapter(project_context)
        self._lock = threading.RLock()
        self._leases = {}
        self._root = Path(root) if root else Path(
            _default_bundle_root(project_context))

    # -- public API ------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def prepare(self, model_id: str, source_revision: int,
                *, appearance_revision: Optional[int] = None) -> PreparedBundle:
        """Materialise a working copy of one model and lease it."""
        payload = self.repository.get_payload(model_id)
        if payload is None:
            raise ModelStorageError(f'模型不在这个工程里：{model_id}')
        revision = int(payload.get('source_revision', 0) or 0)
        if int(source_revision) != revision:
            raise ModelStorageError(
                f'模型原件修订不一致（存储 {revision}，请求 {source_revision}）')
        key = self._key(model_id, revision, appearance_revision)
        lease_id = uuid.uuid4().hex
        package_root = self._root / lease_id
        try:
            self._materialise(payload, package_root)
        except BaseException:
            shutil.rmtree(package_root, ignore_errors=True)
            raise
        manifest_path = package_root / MANIFEST_NAME
        with self._lock:
            self._leases[lease_id] = {
                'package_root': str(package_root),
                'key': key,
                'model_id': model_id,
                'source_revision': revision,
                'appearance_revision': appearance_revision,
            }
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        main_relative_path = manifest.get('main_relative_path', '')
        return PreparedBundle(
            lease_id=lease_id,
            package_root=str(package_root),
            main_relative_path=main_relative_path,
            manifest_path=str(manifest_path),
            source_revision=revision,
            appearance_revision=appearance_revision,
            model_id=model_id,
            key=key,
            manifest=manifest)

    def release(self, lease_id: str) -> None:
        """Give the lease back and remove the working copy."""
        with self._lock:
            lease = self._leases.pop(lease_id, None)
        if lease is None:
            raise BundleLeaseError(f'未知的租约：{lease_id}')
        root = Path(lease['package_root'])
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)

    def release_all(self) -> None:
        with self._lock:
            leases = list(self._leases)
        try:
            from .events import record_model_event
            record_model_event('model_leases_released', count=len(leases),
                               state='released')
        except Exception:                              # noqa: BLE001 - logging only
            pass
        for lease_id in leases:
            try:
                self.release(lease_id)
            except BundleLeaseError:
                pass

    def leased(self) -> tuple:
        """Every lease this store still holds, as ``(lease_id, details)``."""
        with self._lock:
            return tuple((lease_id, dict(details))
                         for lease_id, details in self._leases.items())

    def is_leased(self, package_root) -> bool:
        """True while a package directory is protected by a lease."""
        target = str(Path(package_root).resolve())
        with self._lock:
            return any(Path(item['package_root']).resolve() == Path(target)
                       for item in self._leases.values()
                       if Path(item['package_root']).exists())

    # -- internals -------------------------------------------------------

    def _key(self, model_id: str, source_revision: int,
             appearance_revision: Optional[int]) -> str:
        key = f'{model_id}:r{source_revision}'
        if appearance_revision is not None:
            key = f'{key}:a{int(appearance_revision)}'
        return key

    def _materialise(self, payload: dict, package_root: Path) -> None:
        package_root.mkdir(parents=True, exist_ok=True)
        files = [item for item in payload.get('files', []) if isinstance(item, dict)]
        entries = []
        absolute_map = []
        for item in files:
            if item.get('missing'):
                entries.append({
                    'file_id': item.get('file_id', ''),
                    'relative_path': '',
                    'role': item.get('role', 'unknown'),
                    'sha256': item.get('sha256', ''),
                    'size_bytes': int(item.get('size_bytes', 0) or 0),
                    'state': 'missing',
                    'original_reference': item.get('original_reference', ''),
                })
                continue
            relative_path = str(item.get('relative_path', ''))
            if not relative_path:
                continue
            target = package_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            size = self._write_original(item, target)
            entries.append({
                'file_id': item.get('file_id', ''),
                'relative_path': relative_path,
                'role': item.get('role', 'unknown'),
                'sha256': item.get('sha256', ''),
                'size_bytes': size,
                'state': 'present',
                'attachment_id': item.get('attachment_id'),
                'original_reference': item.get('original_reference', ''),
                'absolute_reference': bool(item.get('absolute_reference')),
            })
            if item.get('absolute_reference'):
                absolute_map.append({
                    'original_reference': item.get('original_reference', ''),
                    'relative_path': relative_path,
                })
        main_file_id = str(payload.get('main_file_id', ''))
        main_relative_path = ''
        for entry in entries:
            if entry['file_id'] == main_file_id and entry['relative_path']:
                main_relative_path = entry['relative_path']
                break
        if not main_relative_path:
            for entry in entries:
                if entry.get('role') == 'main' and entry['relative_path']:
                    main_relative_path = entry['relative_path']
                    break
        work_copies = self._write_work_copies(package_root, entries, absolute_map)
        manifest = {
            'model_id': payload.get('model_id', ''),
            'name': payload.get('name', ''),
            'format': payload.get('format', ''),
            'source_revision': int(payload.get('source_revision', 0) or 0),
            'appearance_revision': int(payload.get('appearance', {}).get('revision', 0) or 0),
            'main_relative_path': main_relative_path,
            'files': entries,
            'absolute_reference_map': absolute_map,
            'work_copies': work_copies,
            'note': ('包内文件是原件的副本；派生的预览资源在 _preview 目录，'
                     '原件本身不会被改写。'),
        }
        (package_root / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    def _write_original(self, item: dict, target: Path) -> int:
        attachment_id = item.get('attachment_id')
        if attachment_id is None:
            raise ModelStorageError(
                f'原件没有受管理引用：{item.get("relative_path")}')
        digest = hashlib.sha256()
        written = 0
        with self.repository.resources.open(int(attachment_id)) as reader:
            with target.open('wb') as handle:
                while True:
                    chunk = reader.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                    written += len(chunk)
        expected = str(item.get('sha256', ''))
        if expected and digest.hexdigest() != expected:
            raise ModelStorageError(
                f'包内副本校验失败：{item.get("relative_path")}')
        return written

    def _write_work_copies(self, package_root: Path, entries, absolute_map) -> list:
        """Derived preview copies for the text formats with absolute paths.

        The original file keeps its bytes.  The work copy is a *derived*
        resource; it lives in its own folder so nobody can mistake it for
        the original.
        """
        if not absolute_map:
            return []
        preview_root = package_root / PREVIEW_DIRECTORY
        preview_root.mkdir(parents=True, exist_ok=True)
        replacements = {item['original_reference']: item['relative_path']
                        for item in absolute_map}
        work_copies = []
        for entry in entries:
            relative_path = entry.get('relative_path', '')
            if not relative_path or entry.get('role') not in ('material', 'main'):
                continue
            if Path(relative_path).suffix.lower() not in ('.mtl', '.obj'):
                continue
            source = package_root / relative_path
            if not source.is_file():
                continue
            text = source.read_bytes().decode('utf-8', 'replace')
            rewritten = text
            changed = False
            for original, target in replacements.items():
                if original and original in rewritten:
                    rewritten = rewritten.replace(original, target)
                    changed = True
            if not changed:
                continue
            target = preview_root / (Path(relative_path).name + '.work')
            target.write_text(rewritten, encoding='utf-8')
            work_copies.append({
                'source_relative_path': relative_path,
                'work_relative_path': f'{PREVIEW_DIRECTORY}/{target.name}',
                'reason': 'absolute references were re-pointed at the package',
            })
        return work_copies


def _default_bundle_root(context: ModelProjectContext) -> str:
    """Where the working copies live for this project."""
    import tempfile
    return str(Path(tempfile.gettempdir()) / BUNDLE_ROOT_NAME /
               context.storage_instance_id.replace(':', '_'))


#: A session directory is named after the session token, which is a uuid4 hex
#: string.  Every other directory under the cache root is a shared cache
#: (``covers``, ``material-work``) and is never reclaimed.
SESSION_DIRECTORY = re.compile(r'^[0-9a-f]{32}$')


def reclaim_stale_sessions(cache_base, current='', *, older_than=3600.0,
                           budget=1.5) -> int:
    """Delete the cache directories of model-library sessions that are gone.

    Every model-library open used to get its own directory under the cache
    root - the session token is a fresh uuid - and a session that ended with
    the application never removed it.  The machine this was measured on held
    **188** such directories and **3.81 GB**, of which 640 MB were working
    copies whose leases were never released: the release runs on a daemon
    thread that the process does not wait for.

    The directories are reclaimed here, in the background, so that opening
    the library never waits for the disk.  ``older_than`` keeps a session
    that is still in use out of the way, and ``budget`` bounds how much one
    open may take back; the rest waits for the next open.
    """
    deadline = time.monotonic() + budget
    try:
        entries = list(Path(cache_base).iterdir())
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if time.monotonic() >= deadline:
            break
        if entry.name == current or not SESSION_DIRECTORY.match(entry.name):
            continue
        try:
            if not entry.is_dir():
                continue
            age = time.time() - entry.stat().st_mtime
        except OSError:
            continue
        if age < older_than:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed
