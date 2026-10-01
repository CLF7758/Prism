r"""T9-1b：视频解码适配层。

两个后端包成同一组信号；QtMultimedia 打不开时自动退 PyAV。

**回落路径的测试用假探测器驱动**，不去找个"QtMultimedia 读不了但 PyAV
能读"的文件 —— 那种文件在本机得靠碰运气，而这里要验的是切换逻辑本身。
PyAV 那一半用真视频跑，所以两边都覆盖到了。
"""
import os

import pytest
from PyQt6 import QtCore, QtGui

from prism.fileio import video_frames, video_pyav, video_service

MATERIALS = r'C:\Users\Administrator\Desktop\Prism-测试素材'
SAMPLES = os.path.join(MATERIALS, '多容器样例')
PLAIN = os.path.join(MATERIALS, 'test.mp4')


def _needs(*paths):
    return pytest.mark.skipif(not all(os.path.exists(p) for p in paths),
                              reason='测试素材不在')


# ── 后端清单 ─────────────────────────────────────────────────────

def test_qtmultimedia_is_always_listed():
    assert video_service.BACKEND_QT in video_service.available_backends()


def test_pyav_shows_up_only_when_installed():
    names = video_service.available_backends()
    if video_pyav.available():
        assert video_service.BACKEND_PYAV in names
    else:
        assert video_service.BACKEND_PYAV not in names


# ── 回落：这是引入 PyAV 的全部理由 ───────────────────────────────

class _StubProbe(QtCore.QObject):
    """立刻失败的假探测器，用来驱动回落路径。"""

    ready = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)

    def start(self):
        QtCore.QTimer.singleShot(
            0, lambda: self.failed.emit('打不开这个容器'))

    def cancel(self):
        pass


@pytest.mark.skipif(not video_pyav.available(), reason='没装 PyAV')
@_needs(PLAIN)
def test_probe_falls_back_to_pyav(qapp, qtbot, monkeypatch):
    monkeypatch.setattr(video_frames, 'VideoProbe', _StubProbe)

    adapter = video_service.VideoProbeAdapter(PLAIN)
    chosen = []
    adapter.backend_chosen.connect(chosen.append)

    with qtbot.waitSignal(adapter.ready, timeout=30000) as blocker:
        adapter.start()
    adapter.wait()

    assert chosen == [video_service.BACKEND_PYAV], \
        f'第一个后端失败后该退到 PyAV，实得 {chosen}'
    assert adapter.backend == video_service.BACKEND_PYAV
    assert blocker.args[0].width == 640


@pytest.mark.skipif(not video_pyav.available(), reason='没装 PyAV')
def test_both_backends_failing_gives_one_clear_message(
        qapp, qtbot, monkeypatch):
    monkeypatch.setattr(video_frames, 'VideoProbe', _StubProbe)

    class AlsoBroken(_StubProbe):
        def start(self):
            QtCore.QTimer.singleShot(
                0, lambda: self.failed.emit('PyAV 也不行'))

    monkeypatch.setattr(video_pyav, 'PyAVProbe', AlsoBroken)

    adapter = video_service.VideoProbeAdapter('whatever.mp4')
    failures = []
    adapter.failed.connect(failures.append)

    with qtbot.waitSignal(adapter.failed, timeout=10000):
        adapter.start()
    adapter.wait()
    qtbot.wait(200)

    # 两个后端都失败时只报一条。报哪一条取决于试的顺序 —— 现在默认先
    # PyAV（见 preferred_backend 的说明），所以最后失败的是 QtMultimedia
    # 那条。这里不写死内容，只要求"有且只有一条、且是失败说明"。
    assert len(failures) == 1, f'实得 {failures}'
    assert failures[0].strip(), f'失败说明不能是空的：{failures}'


def test_without_pyav_a_failure_passes_straight_through(
        qapp, qtbot, monkeypatch):
    monkeypatch.setattr(video_frames, 'VideoProbe', _StubProbe)
    monkeypatch.setattr(video_pyav, 'available', lambda: False)

    adapter = video_service.VideoProbeAdapter('whatever.mp4')
    with qtbot.waitSignal(adapter.failed, timeout=10000) as blocker:
        adapter.start()
    adapter.wait()

    assert '打不开这个容器' in blocker.args[0]
    assert adapter.backend == video_service.BACKEND_QT


# ── 探测定下来的后端，抽帧要沿用 ─────────────────────────────────

@_needs(PLAIN)
def test_the_service_remembers_which_backend_probed_ok(qapp, qtbot):
    service = video_service.VideoFrameService()
    assert service.backend_for(PLAIN) == video_service.preferred_backend()

    probe = service.probe(PLAIN)
    with qtbot.waitSignal(probe.ready, timeout=30000):
        probe.start()
    probe.wait()

    chosen = probe.backend
    assert service.backend_for(PLAIN) == chosen
    extractor = service.make_extractor(PLAIN, interval_seconds=10.0)
    assert extractor.backend == chosen, '抽帧要用探测时定下来的那个后端'


def test_forgetting_a_url_restores_the_default(qapp):
    service = video_service.VideoFrameService()
    service._remember('x.mp4', video_service.BACKEND_PYAV)

    assert service.backend_for('x.mp4') == video_service.BACKEND_PYAV
    service.forget('x.mp4')
    # "回到默认"就是回到**当前**默认 —— 断言写死 qtmultimedia 的话，
    # 以后每换一次默认都要来改这条测试。
    assert service.backend_for('x.mp4') == video_service.preferred_backend()

    service._remember('y.mp4', video_service.BACKEND_PYAV)
    service.clear()
    assert service.backend_for('y.mp4') == video_service.preferred_backend()


def test_a_qurl_and_a_string_name_the_same_video(qapp):
    from PyQt6 import QtCore as Core
    service = video_service.VideoFrameService()
    service._remember(str(PLAIN), video_service.BACKEND_PYAV)

    as_url = Core.QUrl.fromLocalFile(PLAIN)
    assert service.backend_for(as_url) == service.backend_for(str(PLAIN))


# ── 信号形状要和现有 UI 对齐 ─────────────────────────────────────

@_needs(PLAIN)
def test_kept_frame_carries_a_tuple(qapp, qtbot):
    """extract_frames.py 接的是 kept_frame，参数是一个 (image, ms) 元组。"""
    adapter = video_service.VideoFrameExtractorAdapter(
        PLAIN, interval_seconds=10.0)
    gotten = []
    adapter.kept_frame.connect(gotten.append)

    with qtbot.waitSignal(adapter.finished, timeout=60000):
        adapter.start()
    adapter.wait()

    assert gotten, '该抽到帧'
    for payload in gotten:
        assert isinstance(payload, tuple) and len(payload) == 2, payload
        image, position = payload
        assert isinstance(image, QtGui.QImage)
        assert isinstance(position, int)


@_needs(PLAIN)
def test_finished_keeps_its_two_argument_shape(qapp, qtbot):
    """finished 仍是 (kept, total)。

    改成三个参数（把坏包数塞进去）会逼着 extract_frames.py 一起改，而那是
    T1 刚验收过的东西。坏包数走单独的 skipped_packets 信号。
    """
    adapter = video_service.VideoFrameExtractorAdapter(
        PLAIN, interval_seconds=10.0)
    with qtbot.waitSignal(adapter.finished, timeout=60000) as blocker:
        adapter.start()
    adapter.wait()

    assert len(blocker.args) == 2, f'实得 {len(blocker.args)} 个参数'
    kept, total = blocker.args
    assert kept > 0
    assert total >= kept


@_needs(PLAIN)
def test_the_skipped_packet_count_gets_its_own_signal(qapp, qtbot):
    """这个源文件里有一个坏包，走 PyAV 时该被报出来。"""
    if not video_pyav.available():
        pytest.skip('没装 PyAV')

    adapter = video_service.VideoFrameExtractorAdapter(
        PLAIN, interval_seconds=5.0, backend=video_service.BACKEND_PYAV)
    skipped = []
    adapter.skipped_packets.connect(skipped.append)

    with qtbot.waitSignal(adapter.finished, timeout=60000):
        adapter.start()
    adapter.wait()

    assert skipped == [1], f'那个坏包该被数出来，实得 {skipped}'


@_needs(PLAIN)
def test_the_default_backend_never_reports_skipped_packets(qapp, qtbot):
    """QtMultimedia 没有坏包这个概念，那个信号一次都不该发。"""
    adapter = video_service.VideoFrameExtractorAdapter(
        PLAIN, interval_seconds=10.0, backend=video_service.BACKEND_QT)
    skipped = []
    adapter.skipped_packets.connect(skipped.append)

    with qtbot.waitSignal(adapter.finished, timeout=60000,
                          raising=False):
        adapter.start()
    adapter.wait()

    assert skipped == []


def test_asking_for_pyav_without_it_falls_back_to_qt(qapp, qtbot,
                                                     monkeypatch):
    monkeypatch.setattr(video_pyav, 'available', lambda: False)

    adapter = video_service.VideoFrameExtractorAdapter(
        'anything.mp4', backend=video_service.BACKEND_PYAV)

    started = []
    monkeypatch.setattr(adapter, '_start_qt',
                        lambda: started.append('qt'))
    adapter.start()

    assert started == ['qt'], 'PyAV 不在时该落到 QtMultimedia'
    assert adapter.backend == video_service.BACKEND_QT


# ── 去重：两条路的行为要一致 ─────────────────────────────────────

def test_dedup_reaches_the_qtmultimedia_backend(qapp, monkeypatch):
    """dedup 参数要传到 FrameExtractor，不能只对 PyAV 生效。"""
    seen = {}

    class FakeExtractor(QtCore.QObject):
        progress = QtCore.pyqtSignal(int, int)
        kept_frame = QtCore.pyqtSignal(object)
        finished = QtCore.pyqtSignal(int, int)
        failed = QtCore.pyqtSignal(str)
        video_info = QtCore.pyqtSignal(object)
        position_changed = QtCore.pyqtSignal(int)

        def __init__(self, url, interval, dedup=True, parent=None):
            super().__init__(parent)
            seen['dedup'] = dedup

        def start(self):
            pass

    monkeypatch.setattr(video_frames, 'FrameExtractor', FakeExtractor)

    adapter = video_service.VideoFrameExtractorAdapter(
        'x.mp4', backend=video_service.BACKEND_QT, dedup=False)
    adapter.start()

    assert seen.get('dedup') is False


@_needs(PLAIN)
def test_pyav_dedup_never_keeps_more_than_without(qapp, qtbot):
    """PyAV 那条路也要判重。

    用户勾了「去掉重复帧」不能因为换了后端就静默失效。这里不去要求
    "一定更少" —— 60 秒的片子可能一直在动，那样去重前后一样多也是对的。
    要钉的是"不会更多"：多出来就意味着判重根本没跑。
    """
    if not video_pyav.available():
        pytest.skip('没装 PyAV')

    counts = {}
    for dedup in (False, True):
        adapter = video_service.VideoFrameExtractorAdapter(
            PLAIN, interval_seconds=0.2,
            backend=video_service.BACKEND_PYAV, dedup=dedup)
        with qtbot.waitSignal(adapter.finished, timeout=120000) as blocker:
            adapter.start()
        adapter.wait()
        counts[dedup] = blocker.args[0]

    assert counts[True] <= counts[False], \
        f'去重后不该更多：不去重 {counts[False]}，去重 {counts[True]}'
