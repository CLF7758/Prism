from PyQt6 import QtCore

from prism.fileio.video_frames import FrameExtractor


def test_frame_extractor_normalizes_local_path_to_qurl(qapp, tmp_path):
    source = tmp_path / 'clip.mp4'

    extractor = FrameExtractor(str(source))

    assert extractor._url.isLocalFile()
    assert extractor._url.toLocalFile().replace('\\', '/') == str(source).replace('\\', '/')


def test_frame_extractor_keeps_qurl(qapp, tmp_path):
    source = tmp_path / 'clip.mp4'
    url = QtCore.QUrl.fromLocalFile(str(source))

    extractor = FrameExtractor(url)

    assert extractor._url == url
