"""Export the original files of one or many models, byte for byte.

The exporter never rewrites a model.  It copies the stored originals into a
directory the user chose, keeps the internal paths of the package, and
verifies every file against the SHA-256 the manifest recorded when the model
was imported.

Rules this implementation keeps:

* the revision and the complete manifest of every model are fixed **before**
  the first byte is written;
* every model gets its own directory; the internal paths of the package are
  preserved; textures subsequently added through the material panel are placed
  alongside the model, with a digest suffix only when names collide;
* the copy goes to a temporary directory first, is verified file by file and
  only then published under its final name;
* a cancelled job removes only its own temporary files -- never the target
  directory, never another job's work;
* a batch that contains a failure is reported as a failure.  "Everything
  succeeded" is never claimed for a batch that did not.

Absolute references inside the original files cannot be repaired without
rewriting the original, which is out of scope.  The export therefore ships a
**path correspondence** next to the model directories
(``_prism_references/<model-id>.json``) that says which original reference
now lives where.
"""
from __future__ import annotations

import hashlib
import json
import os

from .events import (EVENT_EXPORT_ITEM_FAILED, EVENT_EXPORT_MANIFEST_FIXED,
                     EVENT_EXPORT_PUBLISHED, EVENT_EXPORT_VALIDATED,
                     EVENT_EXPORT_WRITTEN, record_model_event)
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional

from .project_storage import (
    CHUNK_SIZE,
    ModelExportCompletedEvent,
    ModelExportFailedEvent,
    ModelExportItemResult,
    ModelExportResult,
    ModelJobPhaseEvent,
    ModelProjectContext,
    ModelStorageError,
)
from .repository import ModelRepositoryAdapter

#: Directory that holds the path correspondence of an export.
REFERENCE_DIRECTORY = '_prism_references'

#: Prefix of the temporary directory of one job.
STAGING_PREFIX = '.prism-model-export-'


class ModelOriginalExporter:
    """Copy the originals of one or many models out of the project."""

    def __init__(self, project_context: ModelProjectContext,
                 repository: ModelRepositoryAdapter,
                 publish_event=None):
        self.context = project_context
        self.repository = repository
        self._publish = publish_event or (lambda event: None)
        self._lock = threading.RLock()
        self._jobs = {}

    # -- public API ------------------------------------------------------

    def export_originals(self, model_ids, destination: str) -> str:
        """Accept an export job.  The returned id is **not** a completion."""
        ids = tuple(str(model_id) for model_id in model_ids)
        if not ids:
            raise ValueError('没有选择要导出的模型')
        target = Path(destination)
        if target.exists() and not target.is_dir():
            raise ValueError('导出目标不是文件夹')
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = {
                'model_ids': ids,
                'destination': str(target),
                'state': 'queued',
                'cancel': threading.Event(),
                'thread': None,
                'session': self.context.session_token,
            }
        thread = threading.Thread(target=self._run, args=(job_id,),
                                  name=f'model-export-{job_id[:8]}', daemon=True)
        with self._lock:
            self._jobs[job_id]['thread'] = thread
        thread.start()
        return job_id

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job['cancel'].set()

    def close_project(self, project_session: str) -> None:
        with self._lock:
            jobs = [(job_id, job) for job_id, job in self._jobs.items()
                    if job.get('session') == project_session]
        for _job_id, job in jobs:
            job['cancel'].set()
        for _job_id, job in jobs:
            thread = job.get('thread')
            if thread is not None and thread.is_alive():
                thread.join(timeout=30)

    def job_state(self, job_id: str) -> Optional[str]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job['state'] if job else None

    # -- worker ----------------------------------------------------------

    def _emit(self, event) -> None:
        try:
            self._publish(event)
        except Exception:  # pragma: no cover - a broken listener is not a
            pass        # failed export

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job['state'] = 'running'
            model_ids = job['model_ids']
            destination = Path(job['destination'])
        # The revision and the complete manifest are fixed up front.
        plans = []
        for model_id in model_ids:
            payload = self.repository.get_payload(model_id)
            if payload is None:
                plans.append((model_id, None))
            else:
                plans.append((model_id, payload))
        total = len(plans)
        # The manifest is fixed here and never re-read later: say so once, so a
        # later "the file disappeared" cannot be confused with this freeze.
        record_model_event(EVENT_EXPORT_MANIFEST_FIXED, job_id=job_id,
                           model_count=total,
                           missing_count=sum(1 for _model_id, payload in plans
                                             if payload is None),
                           destination_name=Path(job['destination']).name)
        items = []
        stopped = False
        for index, (model_id, payload) in enumerate(plans):
            if stopped or job['cancel'].is_set():
                # Everything after the stop is reported as cancelled, never as
                # done: the caller must not have to guess what happened.
                stopped = True
                items.append(ModelExportItemResult(
                    model_id=model_id,
                    name=str((payload or {}).get('name', model_id)),
                    state='cancelled'))
                continue
            if payload is None:
                items.append(ModelExportItemResult(
                    model_id=model_id, name=model_id, state='missing',
                    error='模型不在这个工程里'))
                continue
            self._emit(ModelJobPhaseEvent(
                job_id=job_id, job_kind='export_originals', state='running',
                completed=index, total=total,
                message=f'正在导出 {payload.get("name", model_id)}'))
            item = self._export_one(job_id, payload, destination, job['cancel'])
            items.append(item)
            if item.state not in ('completed', 'cancelled'):
                record_model_event(EVENT_EXPORT_ITEM_FAILED, job_id=job_id,
                                   model_id=model_id,
                                   state=item.state,
                                   error_code=str(getattr(item, 'error', '') or 'export_failed')[:80],
                                   missing_count=len(getattr(item, 'missing', ()) or ()))
            if item.state == 'cancelled':
                stopped = True
        failed = [item for item in items if item.state == 'failed']
        missing = [item for item in items if item.state == 'missing']
        cancelled = [item for item in items if item.state == 'cancelled']
        if cancelled:
            state = 'cancelled'
        elif failed or missing:
            state = 'failed'
        else:
            state = 'completed'
        result = ModelExportResult(job_id=job_id, destination=str(destination),
                                   state=state, items=tuple(items))
        self._emit(ModelExportCompletedEvent(job_id=job_id, result=result))
        self._finish(job_id, state)

    def _finish(self, job_id: str, state: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job['state'] = state

    def _export_one(self, job_id: str, payload: dict, destination: Path,
                    cancel) -> ModelExportItemResult:
        model_id = str(payload.get('model_id', ''))
        name = str(payload.get('name', model_id))
        files = _files_with_material_textures(payload)
        usable = [item for item in files if not item.get('missing')]
        missing = tuple(item.get('original_reference', '')
                        for item in files if item.get('missing'))
        if not usable:
            return ModelExportItemResult(
                model_id=model_id, name=name, state='missing',
                error='模型没有可导出的原件', missing=missing)
        directory_name = _safe_directory_name(name)
        final = destination / directory_name
        staging = destination / f'{STAGING_PREFIX}{job_id[:8]}-{model_id[:8]}'
        created_staging = False
        try:
            if final.exists():
                raise ModelStorageError(f'导出目标已存在：{final}')
            destination.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            created_staging = True
            written = 0
            count = 0
            for item in usable:
                if cancel.is_set():
                    raise _Cancelled()
                target = staging / item['relative_path']
                target.parent.mkdir(parents=True, exist_ok=True)
                size = self._copy_verified(item, target)
                written += size
                count += 1
                record_model_event(EVENT_EXPORT_WRITTEN, job_id=job_id,
                                   model_id=model_id,
                                   relative_path=item['relative_path'],
                                   size_bytes=size, file_index=count,
                                   state='written')
            record_model_event(EVENT_EXPORT_VALIDATED, job_id=job_id,
                               model_id=model_id, file_count=count,
                               bytes_written=written, digest_verified=True,
                               missing_count=len(missing), state='validated')
            if cancel.is_set():
                raise _Cancelled()
            # Every file is on disk and verified; publish the directory.
            os.rename(staging, final)
            created_staging = False
            self._write_references(destination, model_id, usable)
            record_model_event(EVENT_EXPORT_PUBLISHED, job_id=job_id,
                               model_id=model_id, file_count=count,
                               bytes_written=written,
                               directory_name=final.name, state='published')
            return ModelExportItemResult(
                model_id=model_id, name=name, state='completed',
                directory=str(final), files=count, bytes_written=written,
                missing=missing)
        except _Cancelled:
            return ModelExportItemResult(
                model_id=model_id, name=name, state='cancelled',
                missing=missing)
        except ModelStorageError as error:
            return ModelExportItemResult(
                model_id=model_id, name=name, state='failed',
                error=str(error), missing=missing)
        except OSError as error:
            return ModelExportItemResult(
                model_id=model_id, name=name, state='failed',
                error=f'写入失败：{error}', missing=missing)
        finally:
            if created_staging and staging.exists():
                # Only this job's own temporary directory is removed.
                shutil.rmtree(staging, ignore_errors=True)

    def _copy_verified(self, item: dict, target: Path) -> int:
        """Copy one stored original and check its hash while it streams."""
        attachment_id = item.get('attachment_id')
        expected = str(item.get('sha256', ''))
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
        if digest.hexdigest() != expected:
            raise ModelStorageError(
                f'原件校验失败：{item.get("relative_path")}')
        if written != int(item.get('size_bytes', written) or written):
            raise ModelStorageError(
                f'原件大小不一致：{item.get("relative_path")}')
        return written

    def _write_references(self, destination: Path, model_id: str,
                          usable) -> None:
        """Write the path correspondence of the absolute references."""
        absolute = [item for item in usable if item.get('absolute_reference')]
        if not absolute:
            return
        folder = destination / REFERENCE_DIRECTORY
        try:
            folder.mkdir(parents=True, exist_ok=True)
            mapping = {
                'model_id': model_id,
                'note': ('原文件里的绝对路径无法在不改写原件的前提下自动修复；'
                         '下面给出原始引用与导出后位置的对照。'),
                'references': [
                    {'original_reference': item.get('original_reference', ''),
                     'exported_relative_path': item.get('relative_path', '')}
                    for item in absolute],
            }
            path = folder / f'{model_id}.json'
            path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        except OSError:
            # The correspondence is a courtesy; its absence must not turn a
            # verified export into a failure.
            pass


class _Cancelled(Exception):
    """The job was cancelled before the directory was published."""


def _files_with_material_textures(payload):
    """Extend the original manifest with project-managed material textures.

    The import manifest deliberately excludes later appearance edits. Export
    still preserves those model bytes, but must also copy the stored textures
    instead of silently leaving them inside the project.
    """
    files = [dict(item) for item in payload.get('files', []) if isinstance(item, dict)]
    seen = {item.get('attachment_id') for item in files if not item.get('missing')}
    block = payload.get('multimaterial') or {}
    resources = list((block.get('resources') or {}).values())
    resources.extend(((payload.get('appearance') or {}).get('bindings') or {}).values())
    for entry in resources:
        if not isinstance(entry, dict):
            continue
        attachment_id = entry.get('attachment_id', entry.get('attachmentId'))
        if attachment_id is not None and attachment_id in seen:
            continue
        seen.add(attachment_id)
        digest = str(entry.get('sha256') or '')
        name = str(entry.get('locator') or entry.get('name') or 'texture').replace('\\', '/').rsplit('/', 1)[-1]
        relative = _safe_directory_name(name)
        occupied = {str(item.get('relative_path','')).casefold():item.get('sha256') for item in files}
        if occupied.get(relative.casefold()) == digest:
            continue
        if relative.casefold() in occupied and occupied[relative.casefold()] != digest:
            stem, suffix = Path(relative).stem, Path(relative).suffix
            relative = stem+'-'+digest[:12]+suffix
        files.append(dict(role='texture', relative_path=relative,
                          attachment_id=attachment_id, sha256=digest,
                          size_bytes=entry.get('size_bytes', entry.get('size', 0)),
                          missing=False))
    return files


def _safe_directory_name(name: str) -> str:
    """A directory name that is safe on every file system."""
    cleaned = ''.join('_' if character in '\\/:*?"<>|\x00' else character
                      for character in (name or 'model').strip())
    cleaned = cleaned.strip(' .')
    return cleaned or 'model'
