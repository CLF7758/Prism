"""T2.5：剪贴板的隐私项。

任务书要求"密码管理器、临时令牌等内容不能默认进入素材库"。这一条以前
没有实现——剪贴板采集照单全收。

做法不是"检测内容像不像密码"（那做不到可靠），而是**不采集来自这些应用
的剪贴板写入**：复制动作发生的那一刻，前台窗口的进程名如果落在排除列表
里就跳过。

局限要写在明处：剪贴板本身不告诉你来源，前台窗口只是近似。后台工具改写
剪贴板时它取不到正确的名字，这时结果为空——**空意味着"不知道"，不等于
"安全"，也不等于"要拦下"**（一律拦下会让这个功能在取不到来源的平台上
整体失效）。所以空值放行。
"""
import pytest
from PyQt6 import QtCore, QtGui

from prism import clipboard_capture as cc


# ── parse_excluded ───────────────────────────────────────────────────

def test_parsing_accepts_commas_semicolons_and_newlines():
    assert cc.parse_excluded('Foo.exe, bar; baz\nqux') == {
        'foo', 'bar', 'baz', 'qux'}


def test_parsing_lowercases_and_drops_the_suffix():
    assert cc.parse_excluded('KeePassXC.EXE') == {'keepassxc'}


def test_parsing_ignores_empty_entries():
    assert cc.parse_excluded('  ,, ; \n , ') == set()
    assert cc.parse_excluded('') == set()
    assert cc.parse_excluded(None) == set()


# ── excluded_applications ────────────────────────────────────────────

def test_the_builtin_list_covers_password_managers():
    names = cc.excluded_applications()
    for expected in ('1password', 'bitwarden', 'keepass', 'keepassxc',
                     'lastpass'):
        assert expected in names, expected


def test_the_setting_adds_to_the_builtin_list():
    class FakeSettings:
        def valueOrDefault(self, key):
            assert key == cc.EXTRA_EXCLUDED
            return 'my-notes-app, shotty'

    names = cc.excluded_applications(FakeSettings())

    assert 'my-notes-app' in names
    assert 'keepass' in names, '设置是加在内置列表之上，不是替换'


def test_a_broken_setting_falls_back_to_the_builtin_list():
    class Broken:
        def valueOrDefault(self, key):
            raise RuntimeError('设置读不出来')

    assert 'bitwarden' in cc.excluded_applications(Broken())


# ── foreground_process_name ──────────────────────────────────────────

def test_the_source_can_be_looked_up_without_exploding():
    """取不到就返回空串，不能抛 —— 它跑在剪贴板信号里。"""
    name = cc.foreground_process_name()
    assert isinstance(name, str)
    assert name == name.lower()
    assert not name.endswith('.exe')


# ── Adapter.is_excluded ──────────────────────────────────────────────

def test_is_excluded_matches_with_and_without_suffix(qapp):
    adapter = cc.ClipboardCaptureAdapter()
    assert adapter.is_excluded('keepass')
    assert adapter.is_excluded('KeePass.exe')
    assert adapter.is_excluded('KeePassXC.EXE')


def test_is_excluded_is_false_for_ordinary_apps(qapp):
    adapter = cc.ClipboardCaptureAdapter()
    for name in ('photoshop', 'chrome', 'explorer', 'prism'):
        assert not adapter.is_excluded(name), name


def test_an_unknown_source_is_not_treated_as_excluded(qapp):
    """空 = 不知道，不是"不安全"，也不是"要拦下"。"""
    adapter = cc.ClipboardCaptureAdapter()
    assert adapter.is_excluded('') is False
    assert adapter.is_excluded(None) is False


def test_the_exclusion_list_can_be_replaced(qapp):
    adapter = cc.ClipboardCaptureAdapter(excluded=['only-this'])
    assert adapter.is_excluded('only-this')
    assert not adapter.is_excluded('keepass'), '替换之后内置的也不再生效'


def test_an_explicit_empty_list_disables_the_guard(qapp):
    """用户把列表清空就是"我什么都想采集"。"""
    adapter = cc.ClipboardCaptureAdapter(excluded=[])
    assert not adapter.is_excluded('keepass')


# ── 真正的行为：排除的应用不进收集箱 ─────────────────────────────────

class _FakeClipboard(QtCore.QObject):
    dataChanged = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self._mime = QtCore.QMimeData()

    def set_mime(self, mime):
        self._mime = mime

    def mimeData(self):                     # noqa: N802 - Qt naming
        return self._mime

    def ownsClipboard(self):                # noqa: N802 - Qt naming
        return False


def _png_mime():
    image = QtGui.QImage(32, 24, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(200, 40, 40))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    mime = QtCore.QMimeData()
    mime.setData('image/png', buffer.data())
    return mime


def test_a_password_manager_copy_never_reaches_the_inbox(qapp):
    adapter = cc.ClipboardCaptureAdapter()
    adapter._clipboard = _FakeClipboard()
    adapter._clipboard.set_mime(_png_mime())

    seen = []
    adapter.snapshot_ready.connect(seen.append)

    adapter._source_app = 'keepassxc'       # 复制那一刻的前台窗口
    adapter._on_debounce()

    assert seen == [], '密码管理器写进剪贴板的内容不该被采集'


def test_a_credential_prompt_copy_never_reaches_the_inbox(qapp):
    adapter = cc.ClipboardCaptureAdapter()
    adapter._clipboard = _FakeClipboard()
    adapter._clipboard.set_mime(_png_mime())

    seen = []
    adapter.snapshot_ready.connect(seen.append)

    adapter._source_app = 'credentialuibroker'
    adapter._on_debounce()

    assert seen == []


def test_an_ordinary_app_copy_does_reach_the_inbox(qapp):
    adapter = cc.ClipboardCaptureAdapter()
    adapter._clipboard = _FakeClipboard()
    adapter._clipboard.set_mime(_png_mime())

    seen = []
    adapter.snapshot_ready.connect(seen.append)

    adapter._source_app = 'photoshop'
    adapter._on_debounce()

    assert len(seen) == 1, '普通应用复制图片仍要正常采集'


def test_the_source_is_sampled_before_the_debounce(qapp, monkeypatch):
    """来源必须在 dataChanged 那一刻取。

    防抖要等 120ms，那时候焦点常常已经移走了，取到的会是别的应用 ——
    这会让排除列表张冠李戴。
    """
    asked = []

    def fake_lookup():
        asked.append(True)
        return 'keepass'

    monkeypatch.setattr(cc, 'foreground_process_name', fake_lookup)

    adapter = cc.ClipboardCaptureAdapter()
    clipboard = _FakeClipboard()
    adapter._clipboard = clipboard
    clipboard.set_mime(QtCore.QMimeData())

    adapter._on_data_changed()

    assert asked == [True], '应该在 dataChanged 里就查来源'
    assert adapter._source_app == 'keepass'
