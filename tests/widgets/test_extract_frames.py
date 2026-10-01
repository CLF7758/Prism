"""T1：抽帧对话框的输出格式。

真实解码依赖媒体后端，无头环境起不来（见 tools/baseline.py 里的说明），
所以这里测的是对话框自己负责的那部分：格式选择、保存时用对了格式、
扩展名和重名策略。

质量 spinbox 已经按用户要求删掉了（JPEG 改成固定值），所以这里只留一条
「它确实不在了」的回归，不再测联动。
"""
import os

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.items import PrismVideoItem
from prism.widgets.extract_frames import ExtractFramesDialog


@pytest.fixture
def dialog(view, tmp_path):
    video = PrismVideoItem(
        video_url=QtCore.QUrl.fromLocalFile(str(tmp_path / 'clip.mp4')),
        filename='clip.mp4')
    video._canvas_id = 'default-canvas'
    view.scene.addItem(video)
    widget = ExtractFramesDialog(view, video)
    yield widget
    widget._running = False
    widget.deleteLater()
    # PrismVideoItem 会为缩略图建 QMediaPlayer / QVideoSink / QAudioOutput。
    # 进程退出时媒体后端还在拆就是一记 0xC0000005 —— 测试全过，退出码却是
    # 崩溃，run_tests_safe.py 只能把它报成 FAIL。
    # itemChange 里的 cleanup 只在 _playing 时才走，而这些测试从没播放过，
    # 所以这里得自己收干净。
    video.cleanup()


def _frame(colour=(180, 90, 40)):
    image = QtGui.QImage(64, 48, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(*colour))
    return image


def test_quality_is_no_longer_offered(dialog):
    """用户要求把质量 spinbox 删掉 —— 它不该再出现在界面上。

    JPEG 仍按固定值存（`extract_frames.JPEG_QUALITY`），导出行为不变。
    """
    assert not hasattr(dialog, 'quality')
    assert not hasattr(dialog, '_update_quality')


def test_default_format_is_lossless_png(dialog):
    """参考图默认走无损，避免用户在不知情的情况下让画面掉质量。"""
    assert dialog.format_choice.currentData() == 'PNG'


def test_saving_as_png_writes_png_files(dialog, tmp_path, monkeypatch):
    dialog.images = [(_frame(), 0), (_frame((20, 150, 90)), 2000)]
    outdir = tmp_path / 'frames'
    outdir.mkdir()
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getExistingDirectory',
        staticmethod(lambda *a, **k: str(outdir)))

    dialog.format_choice.setCurrentIndex(0)
    dialog._save_images()

    written = sorted(p.name for p in outdir.glob('*'))
    assert len(written) == 2
    assert all(name.endswith('.png') for name in written)
    for path in outdir.glob('*.png'):
        image = QtGui.QImage(str(path))
        assert not image.isNull(), f'{path.name} 读不回来'


def test_saving_as_jpeg_writes_jpg_files(dialog, tmp_path, monkeypatch):
    dialog.images = [(_frame(), 0)]
    outdir = tmp_path / 'frames'
    outdir.mkdir()
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getExistingDirectory',
        staticmethod(lambda *a, **k: str(outdir)))

    dialog.format_choice.setCurrentIndex(1)
    dialog._save_images()

    written = list(outdir.glob('*'))
    assert len(written) == 1
    assert written[0].suffix == '.jpg'


def test_frame_names_carry_the_timestamp(dialog, tmp_path, monkeypatch):
    """文件名要带时间点，否则去重后重新编号就把「第几秒」丢了。"""
    dialog.images = [(_frame(), 0), (_frame(), 65000)]
    outdir = tmp_path / 'frames'
    outdir.mkdir()
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getExistingDirectory',
        staticmethod(lambda *a, **k: str(outdir)))

    dialog._save_images()

    names = sorted(p.name for p in outdir.glob('*'))
    assert any('0000000000ms' in name for name in names)
    assert any('0000065000ms' in name for name in names)


def test_existing_files_are_not_overwritten(dialog, tmp_path, monkeypatch):
    """重名时追加序号，绝不覆盖已经存在的文件。"""
    outdir = tmp_path / 'frames'
    outdir.mkdir()
    existing = outdir / 'frame_0001_0000000000ms.png'
    existing.write_bytes(b'do not touch')

    dialog.images = [(_frame(), 0)]
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getExistingDirectory',
        staticmethod(lambda *a, **k: str(outdir)))
    dialog._save_images()

    assert existing.read_bytes() == b'do not touch', '原文件被覆盖了'
    assert len(list(outdir.glob('*.png'))) == 2


def test_cancelling_the_folder_picker_saves_nothing(dialog, tmp_path,
                                                    monkeypatch):
    dialog.images = [(_frame(), 0)]
    outdir = tmp_path / 'frames'
    outdir.mkdir()
    monkeypatch.setattr(
        QtWidgets.QFileDialog, 'getExistingDirectory',
        staticmethod(lambda *a, **k: ''))

    dialog._save_images()

    assert list(outdir.glob('*')) == []


def test_save_button_starts_disabled(dialog):
    """还没抽到帧时不该能点保存。"""
    assert not dialog.save_button.isEnabled()
