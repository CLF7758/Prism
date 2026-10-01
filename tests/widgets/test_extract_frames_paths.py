"""T1：抽帧的取消、错误与视频参数显示。

真实解码需要媒体后端，跑起来慢且依赖机器上的视频文件；
这个文件只测对话框和抽取器自己负责的那部分 ——
参数怎么显示、进度怎么写时间、出错和取消时会发生什么。

真实视频的端到端验收在 ``tools/t1_video_acceptance.py``。
"""
import pytest
from PyQt6 import QtCore

from prism.fileio.video_frames import (FrameExtractor, VideoInfo, VideoProbe,
                                       format_timestamp)
from prism.widgets.extract_frames import ExtractFramesDialog


class FakeVideoItem:
    """Just enough of ``PrismVideoItem`` for the dialog.

    The real item is deliberately not used here.  A ``PrismVideoItem``
    that has been added to a scene leaves the process unstable: pytest
    reports every test as passed and then the interpreter dies with an
    access violation (0xC0000005).  ``run_tests_safe.py`` can only render
    that as "[FAIL] ... 20 passed", which hides the real result.  The
    crash reproduces without this dialog at all, so it belongs to the
    item, not to frame extraction; ``tools/`` holds the write-up.

    The dialog only ever reads these two attributes.
    """

    TYPE = 'video'

    def __init__(self, url, filename='clip.mp4'):
        self._video_url = url
        self.filename = filename


@pytest.fixture
def video(tmp_path):
    return FakeVideoItem(
        QtCore.QUrl.fromLocalFile(str(tmp_path / 'clip.mp4')))


@pytest.fixture
def dialog(view, video):
    widget = ExtractFramesDialog(view, video)
    yield widget
    widget._stop_work()
    widget.deleteLater()


# ── format_timestamp ─────────────────────────────────────────

def test_timestamps_read_as_minutes_and_seconds():
    assert format_timestamp(0) == '00:00'
    assert format_timestamp(5_000) == '00:05'
    assert format_timestamp(65_000) == '01:05'


def test_long_videos_switch_to_hours():
    """26 分钟的视频不能显示成 26:00 之后继续堆分钟数。"""
    assert format_timestamp(1_570_000) == '26:10'
    assert format_timestamp(3_725_000) == '1:02:05'


def test_negative_positions_clamp_instead_of_going_backwards():
    assert format_timestamp(-1) == '00:00'


# ── VideoInfo ────────────────────────────────────────────────

def test_video_info_has_no_resolution_text_when_decoder_is_silent():
    assert VideoInfo().resolution_text() == ''


def test_video_info_reports_resolution_when_known():
    assert VideoInfo(width=1920, height=1040).resolution_text() == '1920×1040'


def test_video_info_defaults_are_empty_not_made_up():
    info = VideoInfo()
    assert info.duration_ms == 0
    assert info.frame_rate == 0.0
    assert info.has_audio is False


# ── What the dialog shows about the video ────────────────────

def test_dialog_describes_length_resolution_and_frame_rate(dialog):
    text = dialog._describe(
        VideoInfo(duration_ms=65_000, width=1920, height=1040,
                  frame_rate=29.97))
    assert '01:05' in text
    assert '1920×1040' in text
    assert '29.97' in text


def test_dialog_says_so_when_the_decoder_knows_nothing(dialog):
    text = dialog._describe(VideoInfo())
    assert text, '不能显示空标签'


def test_dialog_shows_video_info_when_it_arrives(dialog):
    dialog._on_info_ready(VideoInfo(duration_ms=60_095, width=640, height=360,
                                    frame_rate=23.962))
    assert '01:00' in dialog.info_label.text()
    assert '640×360' in dialog.info_label.text()


def test_an_empty_reading_does_not_wipe_a_good_one(dialog):
    """抽取器有时读不到元数据，不能把已经显示出来的参数抹掉。"""
    dialog._on_info_ready(VideoInfo(duration_ms=60_095, width=640, height=360))
    before = dialog.info_label.text()

    dialog._on_info_ready(VideoInfo())

    assert dialog.info_label.text() == before


def test_a_failed_probe_explains_itself(dialog):
    dialog._on_info_failed('找不到视频文件：clip.mp4')
    assert 'clip.mp4' in dialog.info_label.text()


# ── Progress reports the moment it is looking at ────────────

def test_progress_names_the_current_moment(dialog):
    dialog._info = VideoInfo(duration_ms=120_000)
    dialog._on_position(65_000)
    dialog._on_progress(3, 60)

    assert '01:05' in dialog.status.text(), dialog.status.text()
    assert '02:00' in dialog.progress.format(), dialog.progress.format()


def test_progress_still_works_without_known_duration(dialog):
    """拿不到总时长时也不能崩，只是不显示时间轴。"""
    dialog._info = None
    dialog._on_position(5_000)
    dialog._on_progress(1, 10)

    assert dialog.status.text()


# ── Cancel ───────────────────────────────────────────────────

def test_closing_the_dialog_cancels_the_probe(dialog):
    class RecordingProbe:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    probe = RecordingProbe()
    dialog._probe = probe

    dialog._stop_work()

    assert probe.cancelled, '关闭时没有取消还在跑的探测'
    assert dialog._probe is None


def test_cancelling_keeps_the_images_already_kept(dialog):
    from PyQt6 import QtGui
    dialog._extractor = FrameExtractor('clip.mp4', 2.0)
    dialog._running = True
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(10, 20, 30))
    dialog.images = [(image, 0)]

    dialog._on_close()

    assert len(dialog.images) == 1, '取消不该丢掉已经取到的画面'
    assert not dialog._running


def test_probe_is_stopped_once_extraction_starts(dialog):
    """两个播放器同时开着会把视频解码两遍。"""
    dialog._info = VideoInfo(duration_ms=1000)
    dialog._on_progress(0, 1)
    dialog.start_button.setEnabled(True)
    dialog.interval.setEnabled(True)

    dialog._start()

    assert dialog._probe is None


# ── Errors users can act on ─────────────────────────────────

def test_failure_message_is_shown_and_controls_come_back(dialog):
    dialog._running = True
    dialog.start_button.setEnabled(False)
    dialog.interval.setEnabled(False)

    dialog._on_failed('找不到视频文件：clip.mp4')

    assert 'clip.mp4' in dialog.status.text()
    assert dialog.start_button.isEnabled(), '出错后要能重试'
    assert dialog.interval.isEnabled()
    assert not dialog._running


def test_missing_file_fails_immediately_without_a_player(qapp, tmp_path):
    """路径失效不该先去问后端，直接给出具体原因。"""
    missing = str(tmp_path / 'not_here.mp4')
    extractor = FrameExtractor(missing, 2.0)
    seen = []
    extractor.failed.connect(seen.append)

    extractor.start()

    assert len(seen) == 1, '没有立刻报错，用户会看到进度条一直转'
    assert 'not_here.mp4' in seen[0]
    assert extractor._player is None, '路径都不存在就不该建播放器'


def test_missing_file_fails_the_probe_too(qapp, tmp_path):
    missing = str(tmp_path / 'gone.mp4')
    probe = VideoProbe(missing)
    seen = []
    probe.failed.connect(seen.append)

    probe.start()

    assert len(seen) == 1
    assert 'gone.mp4' in seen[0]


def test_video_without_source_cannot_be_started(view):
    widget = ExtractFramesDialog(view, FakeVideoItem(None, 'none.mp4'))
    try:
        assert not widget.start_button.isEnabled()
        assert 'no readable source' in widget.status.text()
        assert widget._probe is None, '没有来源就不该去探测'
    finally:
        widget._stop_work()
        widget.deleteLater()
