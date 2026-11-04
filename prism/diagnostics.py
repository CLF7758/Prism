"""Bounded, opt-in diagnostics ZIP. No settings imports or project BLOB reads."""
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile
import zipfile

FILE_PATTERN = re.compile(r'(?:Prism\.log(?:\.\d+)?|Prism-operations-\d+\.jsonl(?:\.\d+)?|'
                          r'Prism-startup-\d+\.jsonl|Prism-crash-\d+\.json|Prism-native-crash-\d+\.log)\Z')
SENSITIVE = re.compile(r'password|secret|authorization|cookie|clipboard|api[_-]?key|access[_-]?token', re.I)
PRIVATE_FIELDS = {'text', 'content', 'body', 'html', 'document', 'prompt', 'token',
                  'data', 'payload', 'raw', 'bytes', 'base64', 'image', 'thumbnail'}
PRIVATE_LINE = re.compile(r'''["']?(?:text|content|body|html|document|prompt|token|data|payload)["']?\s*[:=]''', re.I)
PATHS = re.compile(r'''(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\t"'<>|]*|/(?:home|Users)/[^\s"'<>]+''')
SOURCES = ('config/settings.py', 'asset_records.py', 'catalog_match_record.py', 'view.py', '__main__.py', 'performance_trace.py', 'project_catalog.py',
           'project_catalog_index.py', 'image_decode.py', 'source_store.py',
           'storage/migration.py', 'storage/dos_repository.py', 'upgrade_preview.py',
           'diagnostics.py', 'widgets/__init__.py', 'actions/storage_workflow.py',
           'storage/leases.py', 'source_read_status.py', 'widgets/detail_panel.py',
           'model_library/events.py', 'model_library/diagnostic_snapshot.py',
           'model_library/contracts.py')
SOURCES += ('library_observability.py','material_service.py','material_textures.py',
            'widgets/material_render.py','widgets/material_library.py',
            'model_library/multimaterial/application.py','components/material_preview.py',
            'fileio/sql.py','fileio/partial_save.py','widgets/material_task.py',
            'widgets/model_material_panel.py','model_library/babylon_backend.py','model_library/render_backend.py',
            'model_library/multimaterial/browser_scene.py','model_library/multimaterial/browser_geometry.py',
            'model_library/multimaterial/browser_material.py','assets/materials/render.js',
            'assets/materials/model-scene.js','assets/materials/preview-profile.json')
SETTING_TYPES = {'Autosave/enabled': bool, 'Autosave/minutes': (int, float),
                 'BrowserCapture/enabled': bool}


def settings_snapshot(settings):
    """Only explicitly named scalar policy values, never enumerate the INI."""
    values = {}
    for key, expected in SETTING_TYPES.items():
        try:
            value = settings.value(key) if settings is not None else None
            # bool is an int subclass; do not accept it as an interval.
            accepted = type(value) is bool if expected is bool else type(value) in expected
            values[key] = (dict(state='available', value=value) if accepted
                           else dict(state='unknown', reason='missing_or_unexpected_type'))
        except Exception as error:
            values[key] = dict(state='unknown', reason=type(error).__name__)
    return dict(basis='explicit_scalar_allowlist; no INI file read', values=values)


def project_stat_snapshot(filename):
    """Worker-only, bounded metadata calls. Never open a project or enumerate it."""
    if not isinstance(filename, str) or not filename:
        return dict(state='unknown', reason='no_project_path')
    result = dict(basis='filesystem_metadata_only; not content hash or I/O bytes',
                  storage_format='unknown', files={})
    for label, path in (('entry', Path(filename)), ('wal', Path(filename+'-wal'))):
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                result['files'][label] = dict(state='not_accounted', reason='link_or_reparse_point')
            elif stat.S_ISREG(info.st_mode):
                result['files'][label] = dict(state='available', size_bytes=info.st_size,
                    mtime_ns=info.st_mtime_ns, device=info.st_dev, inode=info.st_ino)
            else:
                result['files'][label] = dict(state='not_accounted', reason='not_regular_file')
        except FileNotFoundError:
            result['files'][label] = dict(state='missing')
        except OSError as error:
            result['files'][label] = dict(state='unknown', reason=type(error).__name__)
    return result


class DiagnosticCancelled(RuntimeError):
    pass


def _check(cancel):
    if cancel is not None and cancel():
        raise DiagnosticCancelled('诊断包导出已取消')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Redactor:
    def __init__(self):
        self.names = {name for name in (os.environ.get('USERNAME'), Path.home().name) if name}

    def string(self, value):
        value = re.sub(r'''https?://[^\s"'<>]+''', lambda match:
                       '<url:' + hashlib.sha256(match.group(0).encode()).hexdigest()[:12] + '>', value)
        def path(match):
            raw = match.group(0)
            suffix = ntpath.splitext(raw)[1]
            if not re.fullmatch(r'\.[A-Za-z0-9]{1,12}', suffix):
                suffix = ''
            return '<path:' + hashlib.sha256(raw.encode()).hexdigest()[:12] + suffix + '>'
        value = PATHS.sub(path, value)
        for name in self.names:
            value = re.sub(r'(?<!\w)' + re.escape(name) + r'(?!\w)', '<user>', value, flags=re.I)
        return value

    def value(self, value):
        if isinstance(value, dict):
            return {str(key): ('<redacted>' if SENSITIVE.search(str(key)) or str(key).lower() in PRIVATE_FIELDS
                               else self.value(item)) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return self.string(value) if isinstance(value, str) else value

    def log(self, raw, *, structured):
        text = raw.decode('utf-8', errors='replace')
        if structured:
            try:
                return json.dumps(self.value(json.loads(text)), ensure_ascii=False).encode('utf-8')
            except (ValueError, RecursionError):
                pass
        output = []
        for line in text.splitlines(keepends=True):
            try:
                output.append(json.dumps(self.value(json.loads(line)), ensure_ascii=False) + '\n')
            except (ValueError, RecursionError):
                # Free-form logs may print a clipboard/credential value inline.
                # Drop the whole such line rather than guess its value boundary.
                output.append('<sensitive log line omitted>\n' if SENSITIVE.search(line) or PRIVATE_LINE.search(line)
                              else self.string(line))
        return ''.join(output).encode('utf-8')


def runtime_snapshot(window=None):
    """Call on the GUI thread. Paths are evidence, not proof of loaded bytes."""
    core = sys.modules.get('PyQt6.QtCore')
    trace = getattr(sys.modules.get('prism.performance_trace'), '_active', None)
    view = getattr(window, 'view', None)
    operation = getattr(view, '_storage_operation', None) or getattr(view, '_save_operation', None)
    return dict(collected_at=utc_now(), pid=os.getpid(), python=platform.python_version(),
        version=getattr(sys.modules.get('prism.constants'), 'VERSION', 'unknown'),
        executable=sys.executable, os=platform.platform(),
        qt=core.qVersion() if core is not None else 'unknown',
        dpr=window.devicePixelRatioF() if window is not None else 'unknown', gpu='unknown',
        session=getattr(trace, 'session', None), operation_id=getattr(operation, 'operation_id', None),
        startup_version_snapshot=getattr(trace, 'version_snapshot', None),
        loaded_modules={name: getattr(module, '__file__', None) for name, module in list(sys.modules.items())
                        if name == 'prism' or name.startswith('prism.')},
        loaded_byte_identity='unknown; module paths do not prove in-memory source bytes',
        settings=settings_snapshot(getattr(view, 'settings', None)),
        project=dict(project_id=getattr(getattr(view, 'scene', None), 'project_id', None),
                     filename=getattr(view, 'filename', None),
                     loaded_items=getattr(getattr(view, 'scene', None), '_item_count', None),
                     count_basis='resident, not whole-project',
                     upgrade_phase=getattr(getattr(view, '_upgrade_preview_session', None), 'phase', None)))


def _log_footprint(directory, paths):
    """How much log is on disk, and how much the rotation policy allows.

    A user who runs out of disk needs this number; the bundle used to say
    nothing about size at all.
    """
    try:
        total = sum(info.st_size for _path, info in paths)
        return {'files': len(paths), 'total_bytes': total,
                'directory_entries': len(list(Path(directory).iterdir())),
                'limits': {'max_log_bytes': 32 * 1024 * 1024,
                           'retained_files': 3},
                'state': 'measured'}
    except Exception as error:                       # noqa: BLE001 - recorded
        return {'state': 'unknown', 'reason': type(error).__name__}


def _rotation_failures():
    """Rotation counters, without importing the logging module eagerly."""
    try:
        from .logging import rotation_failures
        return rotation_failures()
    except Exception as error:                       # noqa: BLE001 - recorded
        return {'failures': None, 'reason': type(error).__name__}


def collect_bundle(directory, destination, *, runtime=None, problem=None, cancel=None,
                   source_root=None, max_file_bytes=8*1024*1024, max_log_bytes=32*1024*1024,
                   model_snapshot=None):
    """Copy allowed logs only, anonymize, hash exported evidence, publish last.

    Limits refer to uncompressed evidence bytes. Active files are snapshots;
    mutation and omitted/truncated evidence are explicitly recorded.
    """
    if max_file_bytes <= 0 or max_log_bytes <= 0:
        raise ValueError('Diagnostic limits must be positive')
    directory, destination = Path(directory), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    redactor = Redactor()
    manifest = dict(schema_version=1, collected_at=utc_now(), files=[], omissions=[],
                    privacy='paths/usernames and known content/credential fields redacted; no originals/settings copied',
                    manifest_self_excluded=True, source_reads='allowed logs and selected Python source only')
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=destination.name+'.', suffix='.partial')
    os.close(fd)
    temporary = Path(name)
    remaining = max_log_bytes
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def write(name, payload, **details):
                archive.writestr(name, payload)
                manifest['files'].append(dict(path=name, bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(), **details))
            def write_json(name, value):
                write(name, json.dumps(redactor.value(value), ensure_ascii=False, indent=2).encode('utf-8'))
            paths = []
            if directory.is_dir():
                for path in directory.iterdir():
                    if not FILE_PATTERN.fullmatch(path.name):
                        continue
                    try:
                        paths.append((path, path.lstat()))
                    except OSError as error:
                        manifest['omissions'].append(dict(name=path.name, reason=type(error).__name__))
            else:
                manifest['omissions'].append(dict(name='logs', reason='directory_missing'))
            paths.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
            log_integrity = {'captured': 0, 'tail_incomplete': [],
                             'read_to_end': 0, 'checked': 0}
            exceptions = []
            for number, (path, info) in enumerate(paths):
                _check(cancel)
                if number >= 64 or remaining <= 0:
                    manifest['omissions'].append(dict(name=path.name, reason='bundle_limit'))
                    continue
                try:
                    capture_started = utc_now()
                    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                        manifest['omissions'].append(dict(name=path.name, reason='link_or_reparse_point'))
                        continue
                    take = min(info.st_size, max_file_bytes, remaining)
                    with path.open('rb') as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise OSError('Log replaced before capture')
                        stream.seek(max(0, info.st_size-take))
                        raw = stream.read(take)
                    after = path.stat()
                    payload = redactor.log(raw, structured=path.suffix == '.json')
                    output_limit = min(max_file_bytes, remaining)
                    output_truncated = len(payload) > output_limit
                    payload = payload[:output_limit]
                    remaining -= max(len(raw), len(payload))
                    # Only a capture that reached the real end of the file can
                    # say anything about the tail; a byte-capped read cannot.
                    at_file_end = take >= info.st_size
                    tail_complete = None
                    if at_file_end:
                        tail_complete = (not raw) or raw.endswith((b'\n', b'\r'))
                        log_integrity['checked'] += 1
                        log_integrity['read_to_end'] += 1
                        if not tail_complete and len(log_integrity['tail_incomplete']) < 8:
                            log_integrity['tail_incomplete'].append(path.name)
                    log_integrity['captured'] += 1
                    # Sanitized stacks travel with the bundle: the brief asks
                    # for the call stack, and a support bundle without one
                    # leaves the reader guessing where it broke.
                    if len(exceptions) < 8 and b'Traceback (most recent call last)' in raw:
                        try:
                            excerpt = raw[-20000:]
                            start = excerpt.rfind(b'Traceback (most recent call last)')
                            chunk = excerpt[start:start + 4000]
                            exceptions.append({
                                'source_file': path.name,
                                'sanitized': redactor.log(chunk, structured=False)
                                                .decode('utf-8', 'replace'),
                            })
                        except Exception:          # noqa: BLE001 - recorded
                            pass
                    write('logs/'+path.name, payload, original_size=info.st_size,
                          capture_started_at=capture_started, capture_finished_at=utc_now(),
                          start_offset=max(0, info.st_size-take), source_sha256_basis='captured_range_only',
                          captured_bytes=len(raw), source_sha256_captured=hashlib.sha256(raw).hexdigest(),
                          truncated=take < info.st_size or output_truncated,
                          read_to_file_end=at_file_end, tail_complete=tail_complete,
                          changed_during_read=(info.st_ino,info.st_size,info.st_mtime_ns) !=
                                              (after.st_ino,after.st_size,after.st_mtime_ns))
                except OSError as error:
                    manifest['omissions'].append(dict(name=path.name, reason=type(error).__name__))
            _check(cancel)
            disk = {}
            for relative in SOURCES:
                if source_root is None:
                    break
                _check(cancel)
                path = Path(source_root)/relative
                try:
                    before = path.stat()
                    if before.st_size > 2*1024*1024 or path.is_symlink():
                        disk[relative] = {'state': 'not_accounted', 'reason': 'size_or_link'}
                        continue
                    with path.open('rb') as stream:
                        raw = stream.read(2*1024*1024 + 1)
                    if len(raw) > 2*1024*1024:
                        disk[relative] = {'state': 'not_accounted', 'reason': 'grew_beyond_limit'}
                        continue
                    after = path.stat()
                    disk[relative] = dict(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                        changed_during_read=(before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns))
                except OSError as error:
                    disk[relative] = {'state': 'unknown', 'reason': type(error).__name__}
            project = (runtime or {}).get('project', {})
            write_json('project-stat.json', project_stat_snapshot(project.get('filename')))
            write_json('runtime.json', runtime or {'state': 'unknown'})
            from prism.library_observability import snapshot as library_snapshot
            write_json('library-evidence.json',library_snapshot())
            write_json('disk-source-manifest.json', disk)
            # The model-library snapshot is metadata only: capabilities, queue
            # accounting, job identity, resource budget and error counters. A
            # snapshot that cannot be produced is recorded as an omission, so
            # a reader never mistakes "not collected" for "nothing happened".
            model_state = dict(state='not_supplied',
                               reason='no model-library snapshot was handed to the bundle')
            if model_snapshot is None:
                manifest['omissions'].append(dict(name='model-diagnostic.json',
                                                  reason='snapshot_not_supplied'))
            else:
                try:
                    write_json('model-diagnostic.json', model_snapshot)
                    model_state = dict(state='available', file='model-diagnostic.json',
                                       reads_model_bytes=False)
                except Exception as error:
                    model_state = dict(state='failed', reason=type(error).__name__)
                    manifest['omissions'].append(dict(name='model-diagnostic.json',
                                                      reason=type(error).__name__))
            write_json('summary.json', dict(schema_version=1, problem=problem or {},
                model_library=model_state,
                collected_at=manifest['collected_at'], omissions=manifest['omissions'],
                native_stack='not_collected; native crash log may contain only Python frames',
                windows_events='not_collected', gpu='unknown', project_metadata='not_queried',
                application_overhead='not_measured',
                settings='explicit_scalar_allowlist in runtime.json; unknown when unavailable',
                privacy_limit='best-effort scrubbing of retained logs; user-authored description is explicitly included',
                log_snapshot='retained files only; loss/rotation gaps require analyzer verification',
                log_footprint=_log_footprint(directory, paths),
                exceptions=exceptions or ['none_found_in_retained_logs'],
                log_integrity=dict(log_integrity,
                    tail_incomplete_meaning='文件读到末尾但最后一行不完整：进程很可能在写入过程中终止',
                    checked_meaning='只统计真正读到文件末尾的抓取；被字节上限截断的抓取不判定尾部',
                    rotation_failures=_rotation_failures(),
                    note='尾部与轮转都是可核实的数字，不再是"需要分析器核实"')))
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        _check(cancel)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.link(temporary, destination)
        return str(destination)
    finally:
        temporary.unlink(missing_ok=True)
