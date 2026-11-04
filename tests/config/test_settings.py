import os
import os.path
import tempfile
import subprocess
import sys
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from PyQt6 import QtCore, QtGui

from prism.config.settings import CommandlineArgs


def test_command_line_args_singleton():
    assert CommandlineArgs() is CommandlineArgs()
    assert CommandlineArgs()._args is CommandlineArgs()._args
    CommandlineArgs._instance = None


@patch('prism.config.settings.parser.parse_args')
def test_command_line_args_with_check_forces_new_parsing(parse_mock):
    args1 = CommandlineArgs()
    args2 = CommandlineArgs(with_check=True)
    parse_mock.assert_called_once()
    assert args1 is not args2
    CommandlineArgs._instance = None


def test_command_line_args_get():
    args = CommandlineArgs()
    assert args.loglevel == 'INFO'
    CommandlineArgs._instance = None


def test_command_line_args_get_unknown():
    args = CommandlineArgs()
    with pytest.raises(AttributeError):
        args.foo
    CommandlineArgs._instance = None


def test_settings_on_startup_sets_alloc_from_settings(settings):
    settings.setValue('Items/image_allocation_limit', 66)
    QtGui.QImageReader.setAllocationLimit(100)
    settings.on_startup()
    assert QtGui.QImageReader.allocationLimit() == 66


def test_settings_on_startup_sets_alloc_from_environment(settings):
    settings.setValue('Items/image_allocation_limit', 66)
    os.environ['QT_IMAGEIO_MAXALLOC'] = '42'
    QtGui.QImageReader.setAllocationLimit(100)
    settings.on_startup()
    assert QtGui.QImageReader.allocationLimit() == 42


def test_settings_set_value_without_callback(settings):
    settings.FIELDS = {'foo/bar': {}}
    settings.setValue('foo/bar', 100)
    assert settings.value('foo/bar') == 100


def test_settings_set_value_with_callback(settings):
    foo_callback = MagicMock()
    settings.FIELDS = {'foo/bar': {'post_save_callback': foo_callback}}
    settings.setValue('foo/bar', 100)
    foo_callback.assert_called_once_with(100)
    assert settings.value('foo/bar') == 100


def test_settings_remove_without_callback(settings):
    settings.FIELDS = {'foo/bar': {}}
    settings.remove('foo/bar')
    assert settings.value('foo/bar') is None


def test_settings_remove_with_callback(settings):
    foo_callback = MagicMock()
    settings.FIELDS = {
        'foo/bar': {'default': 66,
                    'post_save_callback': foo_callback}
    }
    settings.remove('foo/bar')
    foo_callback.assert_called_once_with(66)
    assert settings.value('foo/bar') is None


def test_settings_value_or_default_gets_default(settings):
    assert settings.valueOrDefault('Items/image_storage_format') == 'best'


def test_settings_value_or_default_gets_overriden_value(settings):
    settings.setValue('Items/image_storage_format', 'png')
    assert settings.valueOrDefault('Items/image_storage_format') == 'png'


def test_settings_value_or_default_gets_default_when_invalid(settings):
    settings.setValue('Items/image_storage_format', 'foo')
    assert settings.valueOrDefault('Items/image_storage_format') == 'best'


def test_settings_value_or_default_casts_value(settings):
    settings.setValue('Items/arrange_gap', '5')
    assert settings.valueOrDefault('Items/arrange_gap') == 5


def test_settings_value_or_default_gets_default_when_cast_error(settings):
    settings.setValue('Items/arrange_gap', 'foo')
    assert settings.valueOrDefault('Items/arrange_gap') == 0


def test_settings_value_changed_when_default(settings):
    assert settings.value_changed('Items/image_storage_format') is False


def test_settings_value_changed_when_chagned(settings):
    settings.setValue('Items/image_storage_format', 'jpg')
    assert settings.value_changed('Items/image_storage_format') is True


def test_settings_restore_defaults_restores(settings):
    settings.setValue('Items/image_storage_format', 'png')
    settings.restore_defaults()
    assert settings.contains('Items/image_storage_format') is False


def test_settings_restore_defaults_leaves_other_settings(settings):
    settings.setValue('foo/bar', 'baz')
    settings.restore_defaults()
    assert settings.contains('foo/bar') is True
    assert settings.value('foo/bar') == 'baz'


def test_settings_recent_files_get_empty(settings):
    settings.get_recent_files() == []


def test_settings_recent_files_get_existing_only(settings):
    with tempfile.NamedTemporaryFile() as f:
        settings.update_recent_files('foo.prism')
        settings.update_recent_files(f.name)
    settings.get_recent_files(existing_only=True) == [f.name]


def test_settings_recent_files_update(settings):
    settings.update_recent_files('foo.prism')
    settings.update_recent_files('bar.prism')
    assert settings.get_recent_files() == [
        os.path.abspath('bar.prism'),
        os.path.abspath('foo.prism')]


def test_settings_recent_files_update_existing(settings):
    settings.update_recent_files('foo.prism')
    settings.update_recent_files('bar.prism')
    settings.update_recent_files('foo.prism')
    assert settings.get_recent_files() == [
        os.path.abspath('foo.prism'),
        os.path.abspath('bar.prism')]


def test_settings_recent_files_update_respects_max_num(settings):
    for i in range(15):
        settings.update_recent_files(f'{i}.prism')

    recent = settings.get_recent_files()
    assert len(recent) == 10
    assert recent[0] == os.path.abspath('14.prism')
    assert recent[-1] == os.path.abspath('5.prism')


def test_real_ini_lock_cannot_block_gui_mirror_reads_writes_or_new_instances(settings, qtbot, monkeypatch):
    from prism.config import settings as module
    settings.setValue('LockProbe/value', 'initial')
    settings.sync()
    path = settings.fileName()
    helper = (
        'from PyQt6.QtCore import QLockFile;import sys;'
        'lock=QLockFile(sys.argv[1]);assert lock.tryLock(1000);'
        'print("LOCKED",flush=True);sys.stdin.readline();lock.unlock()')
    child = subprocess.Popen([sys.executable, '-c', helper, path + '.lock'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    entered = threading.Event()
    original = module._write_settings

    def write(*args):
        if args[0] == path:
            entered.set()
        return original(*args)

    monkeypatch.setattr(module, '_write_settings', write)
    beats = []
    timer = QtCore.QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(time.perf_counter()))
    try:
        assert child.stdout.readline().strip() == 'LOCKED'
        settings.setValue('LockProbe/value', 'pending')
        qtbot.waitUntil(entered.is_set, timeout=3000)
        timer.start()
        qtbot.wait(80)
        started = time.perf_counter()
        second = module.PrismSettings()
        assert second.fileName() == path
        for number in range(8):
            settings.setValue('LockProbe/value', str(number))
            assert second.value('LockProbe/value') == str(number)
        elapsed = time.perf_counter() - started
        assert elapsed < .1, 'GUI access must not wait for the held real INI lock'
        assert len(beats) >= 4, 'the GUI heartbeat must continue during persistence'
        assert child.poll() is None, 'the lock must still be held during GUI access'
    finally:
        timer.stop()
        if child.poll() is None:
            child.stdin.write('\n')
            child.stdin.flush()
        child.communicate(timeout=10)
        settings.sync()
    disk = QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat)
    disk.sync()
    assert disk.value('LockProbe/value') == '7'


def test_mirror_preserves_qt_arrays_groups_bytes_and_remove_on_disk(settings):
    path = settings.fileName()
    settings.beginGroup('Types')
    settings.setValue('payload', QtCore.QByteArray(b'\x00\xffbytes'))
    settings.setValue('names', ['wood', 'stone'])
    settings.setValue('discard/me', 'old')
    settings.endGroup()
    settings.beginWriteArray('RecentFiles')
    for index, name in enumerate(['one.prism', 'two.prism']):
        settings.setArrayIndex(index)
        settings.setValue('path', name)
    settings.endArray()
    settings.remove('Types/discard')
    settings.sync()
    disk = QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat)
    disk.sync()
    assert disk.value('Types/payload') == QtCore.QByteArray(b'\x00\xffbytes')
    assert disk.value('Types/names') == ['wood', 'stone']
    assert not disk.contains('Types/discard/me')
    assert disk.beginReadArray('RecentFiles') == 2
    for index, expected in enumerate(['one.prism', 'two.prism']):
        disk.setArrayIndex(index)
        assert disk.value('path') == expected
    disk.endArray()


def test_delta_persistence_retains_another_process_unknown_key(settings):
    settings.setValue('Local/value', 'before')
    settings.sync()
    script = ('from PyQt6.QtCore import QSettings;import sys;'
              's=QSettings(sys.argv[1],QSettings.Format.IniFormat);'
              's.setValue("OtherProcess/future-key","keep-me");s.sync();'
              'assert s.status()==QSettings.Status.NoError')
    subprocess.run([sys.executable, '-c', script, settings.fileName()],
                   check=True, capture_output=True, timeout=10)
    settings.setValue('Local/value', 'after')
    settings.sync()
    reader = QtCore.QSettings(settings.fileName(), QtCore.QSettings.Format.IniFormat)
    reader.sync()
    assert reader.value('OtherProcess/future-key') == 'keep-me'
    assert reader.value('Local/value') == 'after'


def test_explicit_sync_imports_external_keys_into_the_gui(settings):
    settings.sync()
    script = ('from PyQt6.QtCore import QSettings;import sys;'
              's=QSettings(sys.argv[1],QSettings.Format.IniFormat);'
              's.setValue("OtherProcess/reload","visible-after-sync");s.sync()')
    subprocess.run([sys.executable, '-c', script, settings.fileName()],
                   check=True, capture_output=True, timeout=10)
    settings.sync()
    assert settings.value('OtherProcess/reload') == 'visible-after-sync'


def test_persistence_failure_is_visible_and_explicit_sync_can_retry(settings, qtbot, monkeypatch):
    from prism.config import settings as module
    settings.sync()
    failures = []
    module.settings_events.persistence_failed.connect(failures.append)
    original = module._write_settings
    try:
        monkeypatch.setattr(module, '_write_settings',
                            lambda *_args: (QtCore.QSettings.Status.AccessError, None))
        settings.setValue('FailureProbe/value', 'kept-in-mirror')
        settings.sync()
        assert settings.status() == QtCore.QSettings.Status.AccessError
        assert settings.value('FailureProbe/value') == 'kept-in-mirror'
        qtbot.waitUntil(lambda: bool(failures), timeout=1000)
        monkeypatch.setattr(module, '_write_settings', original)
        settings.sync()
        assert settings.status() == QtCore.QSettings.Status.NoError
        disk = QtCore.QSettings(settings.fileName(), QtCore.QSettings.Format.IniFormat)
        disk.sync()
        assert disk.value('FailureProbe/value') == 'kept-in-mirror'
    finally:
        module.settings_events.persistence_failed.disconnect(failures.append)


def test_normal_qt_exit_drains_settings_without_an_explicit_sync(tmp_path):
    script = '''
from PyQt6.QtCore import QCoreApplication,QTimer,QByteArray
from prism.config.settings import PrismSettings
import sys
app=QCoreApplication([])
PrismSettings.get_settings_dir=lambda self:sys.argv[1]
s=PrismSettings()
s.setValue('ExitProbe/payload',QByteArray(b'complete'))
print(s.fileName(),flush=True)
QTimer.singleShot(0,app.quit)
app.exec()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, check=True, timeout=15)
    path = result.stdout.strip().splitlines()[-1]
    reader = QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat)
    reader.sync()
    assert reader.value('ExitProbe/payload') == QtCore.QByteArray(b'complete')


def test_the_drain_skips_the_write_when_the_application_is_gone(monkeypatch, settings):
    """Qt 没了之后再建 QSettings 一定失败：那种写入要跳过，而不是记一条异常。

    实测：pytest 收尾时 Qt 已经销毁，drain 去写盘 →
    ``RuntimeError: wrapped C/C++ object of type QSettings has been deleted``，
    那次改动就此丢失（`_persist_settings` 把待写项删掉，不再重试）。
    """
    import prism.config.settings as module
    original_alive = module._application_alive
    monkeypatch.setattr(module, '_application_alive', lambda: False)
    settings.setValue('DrainProbe/value', 'kept-in-mirror')
    module._drain_settings_sync()          # 不抛异常，也不假装写成功
    assert settings.value('DrainProbe/value') == 'kept-in-mirror'
    assert settings.status() == QtCore.QSettings.Status.AccessError
    monkeypatch.setattr(module, '_application_alive', original_alive)
    settings.sync()
    assert settings.status() == QtCore.QSettings.Status.NoError
    disk = QtCore.QSettings(settings.fileName(), QtCore.QSettings.Format.IniFormat)
    disk.sync()
    assert disk.value('DrainProbe/value') == 'kept-in-mirror'
