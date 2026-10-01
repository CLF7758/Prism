r"""T9-1：PyAV 后端（探测 + 抽帧 + 坏包容忍 + 取消）。

真实视频测试。素材在 `Prism-测试素材\`：

  test.mp4              60 秒，h264 640x360 + aac 音频
  多容器样例/sample.mkv  转封装
  多容器样例/sample.mov  转封装
  多容器样例/truncated.mp4  故意截断的坏视频
  多容器样例/long-8min.mp4  7.7 分钟长视频

素材不在时跳过，别的机器上不会误报。每个测试结束都等线程收干净 ——
留着跑完的线程不管，进程会在退出阶段带着崩溃码收场。
"""
import os

import pytest
from PyQt6 import QtGui

from prism.fileio import video_pyav

MATERIALS = r'C:\Users\Administrator\Desktop\Prism-测试素材'
SAMPLES = os.path.join(MATERIALS, '多容器样例')
PLAIN = os.path.join(MATERIALS, 'test.mp4')
MKV = os.path.join(SAMPLES, 'sample.mkv')
MOV = os.path.join(SAMPLES, 'sample.mov')
BROKEN = os.path.join(SAMPLES, 'truncated.mp4')
LONG = os.path.join(SAMPLES, 'long-8min.mp4')

needs_pyav = pytest.mark.skipif(not video_pyav.PYAV_AVAILABLE,
                                reason='没装 PyAV')


def _needs(*paths):
    return pytest.mark.skipif(not all(os.path.exists(p) for p in paths),
                              reason='测试素材不在')


# ── 基本形状 ─────────────────────────────────────────────────────

@needs_pyav
def test_pyav_reports_itself_available():
    assert video_pyav.available() is True
    assert video_pyav.unavailable_reason() == ''


@needs_pyav
def test_local_path_accepts_both_a_url_and_a_string():
    from PyQt6 import QtCore
    assert video_pyav.local_path(PLAIN) == PLAIN
    # QUrl.toLocalFile() 在 Windows 上返回正斜杠。路径本身是对的（PyAV 照样
    # 打得开，探测测试都过了），所以这里比归一化后的结果 —— 不然这条测试
    # 只是在考 Qt 的分隔符风格。
    assert os.path.normpath(
        video_pyav.local_path(QtCore.QUrl.fromLocalFile(PLAIN))) == \
        os.path.normpath(PLAIN)
    assert video_pyav.local_path(None) == ''


@needs_pyav
def test_frame_to_qimage_returns_a_real_image(qapp):
    """造一帧 numpy 数据走转换路径，不依赖解码器。"""
    import numpy as np

    class FakeFrame:
        def to_ndarray(self, format='rgb24'):
            array = np.zeros((4, 6, 3), dtype=np.uint8)
            array[:, :, 0] = 200          # 红
            return array

    image = video_pyav.frame_to_qimage(FakeFrame())

    assert image.width() == 6
    assert image.height() == 4
    assert image.pixelColor(0, 0).red() == 200


# ── 探测 ─────────────────────────────────────────────────────────

@needs_pyav
@_needs(PLAIN)
def test_probing_a_plain_mp4(qapp, qtbot):
    probe = video_pyav.PyAVProbe(PLAIN)
    with qtbot.waitSignal(probe.ready, timeout=30000) as blocker:
        probe.start()
    info = blocker.args[0]
    probe.wait()

    assert info.width == 640
    assert info.height == 360
    assert 55_000 < info.duration_ms < 65_000, f'时长 {info.duration_ms}'
    assert 20 < info.frame_rate < 30
    assert info.has_audio is True, '这个样例带 aac 音轨'
    assert info.resolution_text() == '640×360'


@needs_pyav
@_needs(MKV, MOV)
def test_probing_other_containers(qapp, qtbot):
    """这正是引入 PyAV 的理由：跨容器。"""
    for path in (MKV, MOV):
        probe = video_pyav.PyAVProbe(path)
        with qtbot.waitSignal(probe.ready, timeout=30000) as blocker:
            probe.start()
        info = blocker.args[0]
        probe.wait()
        assert info.width == 640, os.path.basename(path)
        assert info.duration_ms > 0, os.path.basename(path)


@needs_pyav
def test_probing_a_missing_file_fails_instead_of_crashing(qapp, qtbot):
    probe = video_pyav.PyAVProbe(os.path.join(SAMPLES, 'not-here.mp4'))
    with qtbot.waitSignal(probe.failed, timeout=10000) as blocker:
        probe.start()
    probe.wait()

    assert '找不到' in blocker.args[0]


# ── 抽帧 ─────────────────────────────────────────────────────────

@needs_pyav
@_needs(PLAIN)
def test_extracting_frames_at_an_interval(qapp, qtbot):
    extractor = video_pyav.PyAVFrameExtractor(PLAIN, interval_seconds=5.0)
    frames = []
    extractor.frame_ready.connect(
        lambda image, position: frames.append((image, position)))

    with qtbot.waitSignal(extractor.finished, timeout=60000) as blocker:
        extractor.start()
    kept, seen, bad = blocker.args
    extractor.wait()

    assert kept == len(frames)
    assert kept >= 5, f'60 秒的片子按 5 秒抽，只拿到 {kept} 帧太少'
    assert seen > kept
    assert bad == 1, f'源文件里那一个坏包该被数出来，实得 {bad}'

    image, position = frames[0]
    assert isinstance(image, QtGui.QImage)
    assert not image.isNull()
    assert image.width() == 640 and image.height() == 360
    assert position >= 0


@needs_pyav
@_needs(PLAIN)
def test_a_longer_interval_yields_fewer_frames(qapp, qtbot):
    counts = []
    for interval in (2.0, 10.0):
        extractor = video_pyav.PyAVFrameExtractor(
            PLAIN, interval_seconds=interval)
        with qtbot.waitSignal(extractor.finished, timeout=60000) as blocker:
            extractor.start()
        extractor.wait()
        counts.append(blocker.args[0])

    assert counts[0] > counts[1], f'2 秒该比 10 秒抽得多，实得 {counts}'


@needs_pyav
@_needs(BROKEN)
def test_a_truncated_video_still_yields_frames(qapp, qtbot):
    """坏视频要么抽出部分帧、要么报清楚的错，不能崩。"""
    extractor = video_pyav.PyAVFrameExtractor(BROKEN, interval_seconds=2.0)
    outcome = {}

    def on_done(kept, seen, bad):
        outcome['done'] = (kept, seen, bad)

    def on_failed(message):
        outcome['failed'] = message

    extractor.finished.connect(on_done)
    extractor.failed.connect(on_failed)

    with qtbot.waitSignal(extractor.finished, timeout=60000,
                          raising=False):
        extractor.start()
    extractor.wait()

    assert 'done' in outcome or 'failed' in outcome, outcome
    if 'done' in outcome:
        kept, seen, bad = outcome['done']
        assert seen > 0, '截断的文件也该能解出前面的帧'
        assert bad >= 1, '该把跳过的坏包数出来'


# ── 取消 ─────────────────────────────────────────────────────────

@needs_pyav
@_needs(LONG)
def test_cancelling_stops_early(qapp, qtbot):
    """7.7 分钟的长视频上取消，应该很快停，而不是抽完。"""
    extractor = video_pyav.PyAVFrameExtractor(LONG, interval_seconds=0.5)
    frames = []
    extractor.frame_ready.connect(lambda image, position: frames.append(1))

    import time
    started = time.perf_counter()
    extractor.start()

    # 抽几帧就取消
    deadline = time.perf_counter() + 30
    while len(frames) < 3 and time.perf_counter() < deadline:
        qtbot.wait(50)
    extractor.cancel()
    extractor.wait(30000)
    elapsed = time.perf_counter() - started

    assert len(frames) >= 1, '取消前该已经出过帧'
    assert elapsed < 25, f'取消后 {elapsed:.1f} 秒才停，太久了'
