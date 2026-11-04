import json
from pathlib import Path
import zipfile

from prism.diagnostics import collect_bundle, project_stat_snapshot, settings_snapshot


def test_settings_reads_only_scalar_allowlist():
    class Settings:
        def __init__(self):
            self.reads = []
        def value(self, key):
            self.reads.append(key)
            return {'Autosave/enabled': True, 'Autosave/minutes': 5,
                    'BrowserCapture/enabled': False}[key]
        def allKeys(self):
            raise AssertionError('must not enumerate private settings')
    settings = Settings()
    result = settings_snapshot(settings)
    assert settings.reads == ['Autosave/enabled', 'Autosave/minutes', 'BrowserCapture/enabled']
    assert result['values']['Autosave/minutes']['value'] == 5


def test_unknown_settings_never_include_unexpected_strings():
    class Settings:
        def value(self, key):
            return 'SECRET-PATH-OR-CONTENT'
    result = settings_snapshot(Settings())
    assert 'SECRET' not in json.dumps(result)
    assert all(row['state'] == 'unknown' for row in result['values'].values())
    assert all(row['state'] == 'unknown' for row in settings_snapshot(None)['values'].values())


def test_stat_does_not_open_or_enumerate_project(tmp_path, monkeypatch):
    project = tmp_path/'legacy.prism'
    project.write_bytes(b'ORIGINAL-BYTES')
    wal = tmp_path/'legacy.prism-wal'
    wal.write_bytes(b'WAL')
    def forbidden(*args, **kwargs):
        raise AssertionError('metadata must not read project bytes or enumerate directories')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(Path, 'iterdir', forbidden)
    result = project_stat_snapshot(str(project))
    assert result['files']['entry']['size_bytes'] == 14
    assert result['files']['wal']['size_bytes'] == 3
    assert result['storage_format'] == 'unknown'


def test_stat_distinguishes_missing_directory_and_unbound(tmp_path):
    assert project_stat_snapshot(None)['state'] == 'unknown'
    assert project_stat_snapshot(str(tmp_path/'missing'))['files']['entry']['state'] == 'missing'
    result = project_stat_snapshot(str(tmp_path))
    assert result['files']['entry']['reason'] == 'not_regular_file'


def test_bundle_includes_hashed_stat_without_original_bytes(tmp_path):
    project = tmp_path/'private.prism'
    project.write_bytes(b'PRIVATE-ORIGINAL-BYTES')
    target = tmp_path/'diagnostics.zip'
    collect_bundle(tmp_path, target, runtime={'project': {'filename': str(project)},
                   'settings': settings_snapshot(None)})
    with zipfile.ZipFile(target) as archive:
        record = json.loads(archive.read('project-stat.json'))
        assert record['files']['entry']['size_bytes'] == len(b'PRIVATE-ORIGINAL-BYTES')
        manifest = json.loads(archive.read('manifest.json'))
        assert any(row['path'] == 'project-stat.json' for row in manifest['files'])
        assert all(b'PRIVATE-ORIGINAL-BYTES' not in archive.read(name) for name in archive.namelist())
        assert str(project).encode() not in archive.read('runtime.json')
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location('safe_metadata_analyzer',
        Path(__file__).resolve().parents[1]/'tools/analyze_prism_diagnostics.py')
    analyzer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = analyzer
    spec.loader.exec_module(analyzer)
    report = analyzer.analyze_bundle(target)
    context = report['diagnostic_bundle']['context']
    assert context['project-stat.json']['files']['entry']['size_bytes'] == 22
    assert context['runtime.json']['settings']['values']['Autosave/enabled']['state'] == 'unknown'
