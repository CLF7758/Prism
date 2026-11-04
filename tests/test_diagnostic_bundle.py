import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from prism.diagnostics import collect_bundle, DiagnosticCancelled


def inspect_bundle(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert len({row['path'] for row in manifest['files']}) == len(manifest['files'])
        for row in manifest['files']:
            data = archive.read(row['path'])
            assert len(data) == row['bytes']
            assert hashlib.sha256(data).hexdigest() == row['sha256']
        assert set(archive.namelist()) == {'manifest.json'} | {r['path'] for r in manifest['files']}
        return manifest, {name: archive.read(name) for name in archive.namelist()}


def test_export_keeps_all_rotation_names_and_hashes_but_no_project_or_settings(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    expected = ['Prism.log', 'Prism.log.1', 'Prism-operations-123.jsonl',
                'Prism-operations-123.jsonl.1', 'Prism-operations-123.jsonl.10',
                'Prism-crash-123.json', 'Prism-native-crash-123.log']
    for name in expected:
        (logs / name).write_text('{"event":"session_start","session":"s1","sequence":1}\n', encoding='utf-8')
    for name in ('Prism.ini', 'private.prism', 'attachments.blob', 'Prism.log.secret', 'packs.idx'):
        (logs / name).write_bytes(b'PRIVATE-ORIGINAL')
    before = {p.name: p.read_bytes() for p in logs.iterdir()}
    path = tmp_path / 'diagnostic.zip'
    collect_bundle(logs, path, runtime={'pid': 123}, problem={'description': '保存后没有提示'})
    manifest, content = inspect_bundle(path)
    assert {name for name in content if name.startswith('logs/')} == {'logs/'+name for name in expected}
    assert not any(b'PRIVATE-ORIGINAL' in value for value in content.values())
    assert {p.name: p.read_bytes() for p in logs.iterdir()} == before
    assert all(not row['truncated'] for row in manifest['files'] if row['path'].startswith('logs/'))
    assert json.loads(content['summary.json'])['native_stack'].startswith('not_collected')


def test_privacy_removes_path_credentials_body_and_clipboard(tmp_path):
    raw = dict(event='problem', filename=r'C:\Users\Somebody\private\original.prism',
               metadata={'password': 'S3CRET', 'access_token': 'TOK3N', 'html': '<p>private</p>'},
               clipboard='CLIPBOARD_PRIVATE', count=37)
    (tmp_path / 'Prism-operations-5.jsonl').write_text(json.dumps(raw)+'\n', encoding='utf-8')
    (tmp_path / 'Prism.log').write_text('Authorization: Bearer S3CRET\nsource C:\\Users\\Somebody\\private.prism\n', encoding='utf-8')
    target = tmp_path / 'out.zip'
    collect_bundle(tmp_path, target)
    _, content = inspect_bundle(target)
    combined = b''.join(content.values())
    for private in (b'S3CRET', b'TOK3N', b'CLIPBOARD_PRIVATE', b'private</p>', b'Somebody', b'C:\\Users'):
        assert private not in combined
    record = json.loads(content['logs/Prism-operations-5.jsonl'])
    assert record['count'] == 37 and record['event'] == 'problem'


def test_limits_record_truncation_and_missing_evidence_honestly(tmp_path):
    (tmp_path / 'Prism.log').write_text('x' * 1000, encoding='utf-8')
    target = tmp_path / 'limited.zip'
    collect_bundle(tmp_path, target, max_file_bytes=128, max_log_bytes=256)
    manifest, content = inspect_bundle(target)
    row = next(row for row in manifest['files'] if row['path'] == 'logs/Prism.log')
    assert row['truncated'] and row['original_size'] == 1000 and row['captured_bytes'] == 128
    assert len(content['logs/Prism.log']) <= 256
    assert json.loads(content['runtime.json'])['state'] == 'unknown'


def test_cancel_removes_partial_and_never_publishes_target(tmp_path):
    target = tmp_path / 'cancelled.zip'
    with pytest.raises(DiagnosticCancelled):
        collect_bundle(tmp_path, target, cancel=lambda: True)
    assert not target.exists()
    assert not list(tmp_path.glob('*.partial'))


def test_existing_destination_is_not_overwritten(tmp_path):
    target = tmp_path / 'existing.zip'
    target.write_bytes(b'original')
    with pytest.raises(FileExistsError):
        collect_bundle(tmp_path, target)
    assert target.read_bytes() == b'original'


def test_disk_version_hash_is_not_claimed_as_loaded_identity(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    raw = b'# current on-disk version\n'
    (source / 'view.py').write_bytes(raw)
    target = tmp_path / 'version.zip'
    collect_bundle(tmp_path, target, source_root=source, runtime={
        'loaded_byte_identity': 'unknown', 'startup_version_snapshot': {'view.py': 'older'}})
    _, content = inspect_bundle(target)
    assert json.loads(content['disk-source-manifest.json'])['view.py']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert json.loads(content['runtime.json'])['loaded_byte_identity'] == 'unknown'


def test_partial_json_and_url_credentials_are_not_exported(tmp_path):
    (tmp_path / 'Prism.log').write_text('"body": "PRIVATE_BODY_WITHOUT_CLOSING_QUOTE\n'
        'request https://someone:PASSWORD@example.com/a?token=URL_TOKEN\n', encoding='utf-8')
    target = tmp_path / 'private.zip'
    collect_bundle(tmp_path, target)
    _, content = inspect_bundle(target)
    raw = b''.join(content.values())
    for value in (b'PRIVATE_BODY_WITHOUT_CLOSING_QUOTE', b'PASSWORD', b'URL_TOKEN'):
        assert value not in raw


def test_disappearing_rotation_is_reported_without_losing_other_logs(tmp_path, monkeypatch):
    first = tmp_path / 'Prism.log'
    first.write_text('still present\n', encoding='utf-8')
    vanished = tmp_path / 'Prism.log.1'
    vanished.write_text('will rotate\n', encoding='utf-8')
    lstat = Path.lstat
    def unstable(path, *args, **kwargs):
        if path == vanished:
            raise FileNotFoundError('rotation removed it')
        return lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', unstable)
    target = tmp_path / 'rotation.zip'
    collect_bundle(tmp_path, target)
    manifest, content = inspect_bundle(target)
    assert 'logs/Prism.log' in content
    assert {'name': 'Prism.log.1', 'reason': 'FileNotFoundError'} in manifest['omissions']


def test_log_dialog_reads_tail_and_exports_from_worker(main_window, qtbot, tmp_path, monkeypatch):
    import threading
    from PyQt6 import QtWidgets
    from prism.widgets import diagnostics_dialog
    log = tmp_path / 'Prism.log'
    log.write_text('旧' * 100000 + '\n最后一条\n', encoding='utf-8')
    monkeypatch.setattr(diagnostics_dialog, 'logfile_name', lambda: str(log))
    real = diagnostics_dialog.collect_bundle
    threads = []
    def collect(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return real(*args, **kwargs)
    monkeypatch.setattr(diagnostics_dialog, 'collect_bundle', collect)
    target = tmp_path / 'dialog.zip'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(target), 'zip'))
    dialog = diagnostics_dialog.DebugLogDialog(main_window)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._preview_job is None)
    assert '最后一条' in dialog.log_txt
    assert len(dialog.log_txt.encode('utf-8')) < 132000
    dialog.mark_problem()
    dialog.description.setPlainText('操作：保存；预期：提示；实际：无提示')
    dialog.export_bundle()
    qtbot.waitUntil(lambda: dialog._export_job is None, timeout=10000)
    assert threads and all(thread != 'MainThread' for thread in threads)
    _, content = inspect_bundle(target)
    assert json.loads(content['summary.json'])['problem']['marker_id'] == dialog.marker['marker_id']
    dialog.reject()


def test_verified_bundle_reuses_trace_analyzer_without_starting_prism(tmp_path):
    from tools.analyze_prism_diagnostics import analyze_bundle
    log = tmp_path / 'Prism-operations-987.jsonl'
    log.write_text('\n'.join(json.dumps(dict(event=event, session='known-session',
        sequence=index, schema_version=2, time=float(index), monotonic=float(index), pid=987))
        for index, event in enumerate(('session_start', 'session_end'), 1)) + '\n', encoding='utf-8')
    target = tmp_path / 'analyze.zip'
    collect_bundle(tmp_path, target)
    report = analyze_bundle(target)
    assert report['diagnostic_bundle']['manifest_verified'] is True
    assert report['diagnostic_bundle']['operation_log_files'] == 1
    assert 'known-session' in json.dumps(report['reports'])
    assert report['diagnostic_bundle']['runtime_disk_equality'] == 'not_proven'


def test_analyzer_refuses_tampered_evidence_and_unsafe_paths(tmp_path):
    from tools.analyze_prism_diagnostics import analyze_bundle
    (tmp_path / 'Prism.log').write_text('captured evidence', encoding='utf-8')
    original = tmp_path / 'original.zip'
    collect_bundle(tmp_path, original)
    with zipfile.ZipFile(original) as archive:
        evidence = {name: archive.read(name) for name in archive.namelist()}
    changed = tmp_path / 'changed.zip'
    with zipfile.ZipFile(changed, 'w') as archive:
        for name, raw in evidence.items():
            archive.writestr(name, b'tampered' if name == 'logs/Prism.log' else raw)
    with pytest.raises(ValueError, match='hash mismatch'):
        analyze_bundle(changed)
    unsafe = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(unsafe, 'w') as archive:
        archive.writestr('../outside', b'not allowed')
    with pytest.raises(ValueError, match='Unsafe'):
        analyze_bundle(unsafe)
    assert not (tmp_path.parent / 'outside').exists()


def test_dialog_close_cancels_worker_without_destroying_running_thread(main_window, qtbot, tmp_path, monkeypatch):
    import threading
    from PyQt6 import QtWidgets
    from prism.widgets import diagnostics_dialog
    log = tmp_path / 'Prism.log'
    log.write_text('capture me', encoding='utf-8')
    monkeypatch.setattr(diagnostics_dialog, 'logfile_name', lambda: str(log))
    entered, release = threading.Event(), threading.Event()
    real = diagnostics_dialog.collect_bundle
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(diagnostics_dialog, 'collect_bundle', held)
    target = tmp_path / 'cancelled-ui.zip'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(target), 'zip'))
    dialog = diagnostics_dialog.DebugLogDialog(main_window)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._preview_job is None)
    try:
        dialog.export_bundle()
        qtbot.waitUntil(entered.is_set)
        dialog.reject()
        assert dialog._export_job.isRunning()
        assert dialog._export_job.isInterruptionRequested()
    finally:
        release.set()
    qtbot.waitUntil(lambda: dialog._export_job is None, timeout=10000)
    assert not target.exists()
    assert not dialog.isVisible()


def test_formal_help_action_opens_new_dialog_and_marker_preserves_trace_clock(main_window, qtbot, tmp_path, monkeypatch):
    from prism import performance_trace
    from prism.widgets.diagnostics_dialog import DebugLogDialog
    import prism.widgets.diagnostics_dialog as module
    monkeypatch.setattr(module, 'logfile_name', lambda: str(tmp_path / 'Prism.log'))
    events = []
    monkeypatch.setattr(performance_trace, 'record', lambda event, **fields: events.append((event, fields)))
    main_window.view.on_action_debuglog()
    dialog = main_window.view.findChild(DebugLogDialog)
    assert dialog is not None and dialog.windowTitle() == '日志与诊断'
    qtbot.waitUntil(lambda: dialog._preview_job is None)
    dialog.mark_problem()
    event, fields = next(row for row in events if row[0] == 'user_problem_marker')
    assert 'time' not in fields and fields['marked_at'] == dialog.marker['time']
    dialog.reject()
