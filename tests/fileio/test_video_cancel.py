"""取消抽帧之后，调用方到底能不能收到信号。

为什么单写这一组：T1 的端到端验收跑到「取消」那一节时，8 分钟的视频
抽到 1.5 秒点了取消，然后**一直等到脚本自己设的 240 秒上限**。查下去
发现 `FrameExtractor.cancel()` 只设了标志加 `_teardown()`，而
`_seek_next()` 看到 `_cancelled` 是**直接 return，一个信号都不发** ——
调用方在等一个信号来决定「进度框什么时候关」，于是永远等不到。

这不是界面冻结（Qt 的事件循环还在转），是任务永远挂着。任务书 T1 的
验收写着「取消任务后窗口不冻结」，这一组就是钉住那条。

PyAV 那条路本来是对的（`request_stop()` + `done.connect(self.finished)`），
这个文件同时确认两条路的行为一致。
"""
import pytest
from PyQt6 import QtCore

from prism.fileio.video_frames import FrameExtractor


def make_extractor(tmp_path, planned=True, kept=0):
    """造一个不接播放器的 extractor。

    `FrameExtractor` 的构造只存 url 和几个计数器，`_player` 是 None，
    `_teardown()` 在那种情况下直接返回 —— 所以不需要真视频、也不需要
    能用的解码器，就能测取消路径。
    """
    url = QtCore.QUrl.fromLocalFile(str(tmp_path / 'whatever.mp4'))
    extractor = FrameExtractor(url, interval_seconds=1.0)
    if planned:
        extractor._timestamps = [0, 1000, 2000, 3000]
    extractor._kept = kept
    return extractor


# ── 核心：取消要发信号 ───────────────────────────────────────────

def test_cancel_emits_finished(qapp, tmp_path):
    extractor = make_extractor(tmp_path, kept=2)
    seen = []
    extractor.finished.connect(lambda kept, total: seen.append((kept, total)))
    extractor.cancel()
    assert seen == [(2, 4)], '取消后必须发 finished，否则调用方永远在等'


def test_cancel_reports_the_frames_already_kept(qapp, tmp_path):
    """取消不是失败：已经交出去的帧要如实报出来。

    任务书 T1：「支持取消，取消后保留已经明确保存的文件」。
    """
    extractor = make_extractor(tmp_path, kept=3)
    seen = []
    extractor.finished.connect(lambda kept, total: seen.append(kept))
    extractor.cancel()
    assert seen == [3]


def test_cancel_before_planning_reports_zero_total(qapp, tmp_path):
    """还没规划时间轴就取消，不能崩，也不能报一个假的总数。"""
    extractor = make_extractor(tmp_path, planned=False)
    seen = []
    extractor.finished.connect(lambda kept, total: seen.append((kept, total)))
    extractor.cancel()
    assert seen == [(0, 0)]


def test_cancel_does_not_emit_failed(qapp, tmp_path):
    """取消不该被当成失败 —— 措辞会误导用户去查「为什么失败」。"""
    extractor = make_extractor(tmp_path, kept=1)
    failures = []
    extractor.failed.connect(failures.append)
    extractor.cancel()
    assert failures == []


# ── 幂等 ────────────────────────────────────────────────────────

def test_cancelling_twice_emits_once(qapp, tmp_path):
    """用户连点两下取消，调用方不该收到两条 finished。"""
    extractor = make_extractor(tmp_path, kept=2)
    seen = []
    extractor.finished.connect(lambda kept, total: seen.append((kept, total)))
    extractor.cancel()
    extractor.cancel()
    extractor.cancel()
    assert len(seen) == 1


def test_cancel_marks_the_extractor_settled(qapp, tmp_path):
    """`_settled` 是防止「取消之后又收到播放器的 error」再发一次信号。"""
    extractor = make_extractor(tmp_path)
    assert extractor._settled is False
    extractor.cancel()
    assert extractor._settled is True


def test_error_after_cancel_is_ignored(qapp, tmp_path):
    """取消之后播放器可能还会吐一个 error（它在被拆除）。

    那个不该再发一次 failed —— 用户已经主动停下了，报错只会让人以为
    自己操作错了。
    """
    from PyQt6 import QtMultimedia

    extractor = make_extractor(tmp_path, kept=1)
    failures = []
    extractor.failed.connect(failures.append)
    extractor.cancel()
    extractor._on_error(QtMultimedia.QMediaPlayer.Error.NoError, '太晚了')
    assert failures == []


# ── 适配层转发 ──────────────────────────────────────────────────

def test_adapter_forwards_cancel_to_the_backend(qapp, tmp_path):
    """适配层的 cancel 要真的传到后端，不能只设自己的标志。

    这里**不能**先调 adapter.start()：无头环境起不来媒体后端，start()
    会立刻撞上 _on_error，_settled 变成 True，随后 cancel() 会被那个
    防重入判断挡掉（那是对的 —— 结束了的任务不该再响应取消）。所以手动
    装一个后端，只验转发。
    """
    from prism.fileio.video_service import (BACKEND_QT,
                                            VideoFrameExtractorAdapter)

    url = QtCore.QUrl.fromLocalFile(str(tmp_path / 'x.mp4'))
    adapter = VideoFrameExtractorAdapter(url, 1.0, BACKEND_QT)
    backend = make_extractor(tmp_path, kept=1)
    adapter._extractor = backend
    adapter.cancel()
    assert backend._cancelled is True
    assert backend._settled is True


def test_adapter_cancel_after_settled_is_a_no_op(qapp, tmp_path):
    """已经结束（报过错或跑完了）之后点取消，不该再发一次信号。"""
    from prism.fileio.video_service import (BACKEND_QT,
                                            VideoFrameExtractorAdapter)

    url = QtCore.QUrl.fromLocalFile(str(tmp_path / 'x.mp4'))
    adapter = VideoFrameExtractorAdapter(url, 1.0, BACKEND_QT)
    backend = make_extractor(tmp_path, kept=1)
    seen = []
    backend.finished.connect(lambda kept, total: seen.append((kept, total)))
    adapter._extractor = backend
    backend._settled = True                      # 假装已经结束了
    adapter.cancel()
    assert seen == []
    assert backend._cancelled is False


def test_adapter_cancel_before_start_does_not_crash(qapp, tmp_path):
    from prism.fileio.video_service import (BACKEND_QT,
                                            VideoFrameExtractorAdapter)

    url = QtCore.QUrl.fromLocalFile(str(tmp_path / 'x.mp4'))
    adapter = VideoFrameExtractorAdapter(url, 1.0, BACKEND_QT)
    adapter.cancel()                    # 还没 start，不该炸
    assert adapter.wait(100) in (True, False)     # 没有后端时返回 True


# ── 顺手钉住载荷的形状 ───────────────────────────────────────────

def test_kept_frame_carries_a_tuple(qapp, tmp_path):
    """`kept_frame` 发的是 (QImage, position_ms) 元组。

    我写验收脚本时在这一处来回错了两次（先多写一个参数，后当成单张
    图），所以在这里写死。信号只有一个参数，那个参数就是元组。
    """
    from PyQt6 import QtGui

    extractor = make_extractor(tmp_path)
    seen = []
    extractor.kept_frame.connect(seen.append)
    image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGB32)
    extractor.kept_frame.emit((image, 1234))
    assert len(seen) == 1
    payload = seen[0]
    assert isinstance(payload, tuple)
    assert payload[1] == 1234
    assert not payload[0].isNull()
