import json
from types import SimpleNamespace
from unittest.mock import Mock

from prism import upgrade_mapping
from prism.actions import storage_workflow
from prism.source_fingerprint import file_fingerprint
from prism.storage import entrypoint, migration


class Settings:
    def __init__(self):
        self.values = {}
    def value(self, key):
        return self.values.get(key)
    def setValue(self, key, value):
        self.values[key] = value


def fake_verified(tmp_path, monkeypatch):
    source, target = tmp_path/'old.prism', tmp_path/'new.prism'
    source.write_bytes(b'ORIGINAL')
    target.write_bytes(b'ENTRY')
    manifest = dict(source=file_fingerprint(source), storage_instance_id='instance-a')
    monkeypatch.setattr(entrypoint, 'is_entry', lambda p: True)
    monkeypatch.setattr(entrypoint, 'resolve', lambda p: tmp_path/'new.prism.data')
    monkeypatch.setattr(migration, 'read_manifest', lambda p: manifest)
    settings = Settings()
    upgrade_mapping.remember(settings, source, target)
    return source, target, manifest, settings


def test_verified_target_reuse_and_changed_instance_rejected(tmp_path, monkeypatch):
    source, target, manifest, settings = fake_verified(tmp_path, monkeypatch)
    hint = upgrade_mapping.hint(settings, source)
    assert upgrade_mapping.validate(source, hint) == str(target.resolve())
    manifest['storage_instance_id'] = 'replacement-instance'
    assert upgrade_mapping.validate(source, hint) is None
    assert source.read_bytes() == b'ORIGINAL'


def test_source_change_wal_and_invalid_hint_rejected(tmp_path, monkeypatch):
    source, target, manifest, settings = fake_verified(tmp_path, monkeypatch)
    hint = upgrade_mapping.hint(settings, source)
    source.with_name(source.name+'-wal').write_bytes(b'NEW-WAL')
    assert upgrade_mapping.validate(source, hint) is None
    assert upgrade_mapping.hint(None, source) is None
    settings.values[upgrade_mapping._key(source)] = 'malformed'
    assert upgrade_mapping.hint(settings, source) is None


def test_corrupt_target_is_not_reused(tmp_path, monkeypatch):
    source, target, manifest, settings = fake_verified(tmp_path, monkeypatch)
    import sqlite3
    def corrupt(path):
        raise sqlite3.DatabaseError('corrupt')
    monkeypatch.setattr(entrypoint, 'resolve', corrupt)
    assert upgrade_mapping.validate(source, upgrade_mapping.hint(settings, source)) is None


def test_existing_target_prompt_reuses_without_conversion(tmp_path, monkeypatch):
    source, target, manifest, settings = fake_verified(tmp_path, monkeypatch)
    view = SimpleNamespace(settings=settings, _trace_open_epoch=7,
                           scene=SimpleNamespace(project_id='p'))
    jobs = []
    monkeypatch.setattr(storage_workflow, 'run', lambda v,t,job,done: jobs.append((job,done)) or True)
    monkeypatch.setattr(storage_workflow.QtWidgets.QMessageBox, 'question',
        lambda *a, **k: storage_workflow.QtWidgets.QMessageBox.StandardButton.Yes)
    opened = Mock()
    monkeypatch.setattr(storage_workflow, '_open_result', opened)
    assert storage_workflow.upgrade_on_open(view, source, readonly=True)
    job, done = jobs.pop()
    done(job(lambda: False))
    opened.assert_not_called()
    job, done = jobs.pop()
    done(job(lambda: False))
    opened.assert_called_once_with(view, str(target), True)


def test_late_validation_cannot_replace_new_project(tmp_path, monkeypatch):
    source, target, manifest, settings = fake_verified(tmp_path, monkeypatch)
    view = SimpleNamespace(settings=settings, _trace_open_epoch=7, scene=SimpleNamespace(project_id='p'))
    jobs = []
    monkeypatch.setattr(storage_workflow, 'run', lambda v,t,job,done: jobs.append((job,done)) or True)
    opened = Mock()
    monkeypatch.setattr(storage_workflow, '_open_result', opened)
    storage_workflow.upgrade_on_open(view, source)
    view._trace_open_epoch += 1
    jobs[0][1](str(target))
    opened.assert_not_called()


def test_modal_confirmation_source_change_refuses_open(tmp_path, monkeypatch):
    source, target, manifest, settings = fake_verified(tmp_path, monkeypatch)
    view = SimpleNamespace(settings=settings, _trace_open_epoch=7, scene=SimpleNamespace(project_id='p'))
    jobs = []
    monkeypatch.setattr(storage_workflow, 'run', lambda v,t,job,done: jobs.append((job,done)) or True)
    def question(*args, **kwargs):
        source.write_bytes(b'CHANGED-SOURCE')
        return storage_workflow.QtWidgets.QMessageBox.StandardButton.Yes
    monkeypatch.setattr(storage_workflow.QtWidgets.QMessageBox, 'question', question)
    warning = Mock()
    monkeypatch.setattr(storage_workflow.QtWidgets.QMessageBox, 'warning', warning)
    opened = Mock()
    monkeypatch.setattr(storage_workflow, '_open_result', opened)
    storage_workflow.upgrade_on_open(view, source)
    job, done = jobs.pop()
    done(job(lambda: False))
    job, done = jobs.pop()
    done(job(lambda: False))
    opened.assert_not_called()
    warning.assert_called_once()
