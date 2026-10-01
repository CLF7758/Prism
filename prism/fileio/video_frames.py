"""Pull still frames out of a video and drop the near-duplicates.

Qt ships an ffmpeg-backed media plugin (``ffmpegmediaplugin.dll``), so no
external ffmpeg binary is required.

QMediaPlayer only delivers frames while an event loop runs on the thread it
lives on, and seeking is asynchronous: ``setPosition`` does not hand back a
frame, it eventually emits one.  That is why this walks one timestamp at a
time and returns to the event loop between steps rather than blocking, and
why it cannot simply run inside the usual worker thread.
"""

import dataclasses
import logging
import os

from PyQt6 import QtCore, QtGui
from PyQt6.QtMultimedia import (QAudioOutput, QMediaMetaData, QMediaPlayer,
                                QVideoSink)

from prism.i18n import _
from prism.similarity import (average_color, color_distance, difference_hash,
                              hamming_distance)

logger = logging.getLogger(__name__)

#: How long to wait for a single seek to produce a frame before giving up.
SEEK_TIMEOUT_MS = 4000

#: Difference-hash distance under which two frames count as the same image.
#: Video frames are compressed, so this is more forgiving than the value
#: used for still-image duplicate detection (4).
DUPLICATE_DISTANCE = 6

#: Colour distance ceiling, matching duplicate_groups().  Structure alone
#: would call two frames identical whenever they happen to share a layout,
#: no matter how different their colours are.
COLOR_DISTANCE = 45

#: How many of the kept frames a new one is compared against.  Comparing
#: within a short window catches A-B-A-B flicker without turning the scan
#: into an all-pairs comparison.
COMPARE_WINDOW = 5


@dataclasses.dataclass(frozen=True)
class VideoInfo:
    """What the decoder can say about a video before frames are pulled.

    Anything the backend does not report stays at zero, and callers show
    only what is actually known rather than printing a made-up number.
    """

    duration_ms: int = 0
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    has_audio: bool = False

    def resolution_text(self):
        if self.width and self.height:
            return '%d×%d' % (self.width, self.height)
        return ''


def format_timestamp(position_ms):
    """Render a position as mm:ss, the form the canvas labels use."""
    total_seconds = max(0, int(position_ms // 1000))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return '%d:%02d:%02d' % (hours, minutes, seconds)
    return '%02d:%02d' % (minutes, seconds)


def _local_path(url):
    """The file behind a local QUrl, or None for anything else."""
    if isinstance(url, QtCore.QUrl) and url.isLocalFile():
        return url.toLocalFile()
    return None


def _info_from_player(player):
    """Build a VideoInfo from a QMediaPlayer that has loaded its media."""
    width = height = 0
    frame_rate = 0.0
    metadata = player.metaData()
    if metadata is not None:
        size = metadata.value(QMediaMetaData.Key.Resolution)
        if size is not None and size.isValid():
            width, height = size.width(), size.height()
        rate = metadata.value(QMediaMetaData.Key.VideoFrameRate)
        if rate is not None:
            try:
                frame_rate = float(rate)
            except (TypeError, ValueError):
                frame_rate = 0.0
    return VideoInfo(
        duration_ms=max(0, int(player.duration())),
        width=width,
        height=height,
        frame_rate=frame_rate,
        has_audio=bool(player.hasAudio()),
    )


class VideoProbe(QtCore.QObject):
    """Read a video's length, pixel size and frame rate without decoding.

    The dialog asks for this as soon as it opens so the interval can be
    picked with the real length in view.  No frames are requested, so the
    cost is a container read rather than a decode.
    """

    ready = QtCore.pyqtSignal(object)       # VideoInfo
    failed = QtCore.pyqtSignal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self._url = url if isinstance(url, QtCore.QUrl) \
            else QtCore.QUrl.fromLocalFile(str(url))
        self._player = None
        self._audio = None
        self._sink = None
        self._settled = False

    def start(self):
        path = _local_path(self._url)
        if path is not None and not os.path.exists(path):
            self._settle_failed(_(
                'The video file could not be found: {name}').format(
                    name=os.path.basename(path)))
            return
        self._player = QMediaPlayer()
        # The audio output and video sink have to be kept alive on self:
        # a backend that is handed only a temporary audio output refuses
        # the media outright, and the sink is what makes the Windows
        # backend load the video track at all.
        self._audio = QAudioOutput()
        self._audio.setMuted(True)
        self._audio.setVolume(0)
        self._sink = QVideoSink()
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._sink)
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.setSource(self._url)
        # Duration and tracks are only reported once playback is asked
        # for; setting the source alone leaves the player short of
        # LoadedMedia.  Nothing is kept of the frames that go past.
        self._player.play()

    def cancel(self):
        self._teardown()

    def _settle_failed(self, message):
        if self._settled:
            return
        self._settled = True
        self._teardown()
        self.failed.emit(message)

    def _on_error(self, error, message=''):
        self._settle_failed(message or _('This video could not be opened.'))

    def _on_status(self, status):
        if self._settled:
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._settled = True
            player = self._player
            info = _info_from_player(player)
            self._teardown()
            self.ready.emit(info)
        elif status in (QMediaPlayer.MediaStatus.InvalidMedia,
                        QMediaPlayer.MediaStatus.NoMedia):
            self._settle_failed(_('This video could not be opened.'))

    def _teardown(self):
        player, audio, sink = self._player, self._audio, self._sink
        self._player = self._audio = self._sink = None
        if player is None:
            return
        try:
            player.setSource(QtCore.QUrl())
        except RuntimeError:
            pass
        player.deleteLater()
        if sink is not None:
            sink.deleteLater()
        if audio is not None:
            audio.deleteLater()


class FrameExtractor(QtCore.QObject):
    """Walk a video at a fixed interval, keeping only the distinct frames."""

    progress = QtCore.pyqtSignal(int, int)      # done, total
    kept_frame = QtCore.pyqtSignal(object)      # (QImage, position_ms)
    finished = QtCore.pyqtSignal(int, int)      # kept, total
    failed = QtCore.pyqtSignal(str)
    video_info = QtCore.pyqtSignal(object)      # VideoInfo, once, on load
    position_changed = QtCore.pyqtSignal(int)   # current position_ms

    def __init__(self, url, interval_seconds=2.0, dedup=True, parent=None):
        super().__init__(parent)
        # QMediaPlayer.setSource() accepts QUrl only.  The dialog normally
        # passes the item's QUrl, but callers such as the browser/import
        # adapters naturally have a local path string.  Normalize both here
        # so the extraction service has one stable boundary.
        if isinstance(url, QtCore.QUrl):
            self._url = url
        else:
            self._url = QtCore.QUrl.fromLocalFile(str(url))
        self._interval_ms = max(1, int(interval_seconds * 1000))
        self._dedup = bool(dedup)
        self._player = None
        self._audio = None
        self._sink = None
        self._timestamps = []
        self._index = 0
        self._kept = 0
        self._hashes = []
        self._waiting = False
        self._cancelled = False
        self._timeout = None
        self._current_image = None
        self._settled = False
        self._planned = False

    # ── Control ───────────────────────────────────────────────

    def start(self):
        """Begin the walk; returns immediately.

        A local file that is not there is reported straight away: asking
        the backend about a missing path costs a round trip and produces a
        vaguer message than the one the user needs.
        """
        path = _local_path(self._url)
        if path is not None and not os.path.exists(path):
            self._settled = True
            self.failed.emit(_(
                'The video file could not be found: {name}').format(
                    name=os.path.basename(path)))
            return
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._audio.setMuted(True)
        self._audio.setVolume(0)
        self._sink = QVideoSink()
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._sink)

        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.setSource(self._url)
        self._player.play()

    def cancel(self):
        """Stop here, and **say** where we stopped.

        Cancelling is not a failure, but it is not silence either: the
        caller is waiting for a signal to know when to close its progress
        dialog.  The original version only set the flag and tore down, and
        ``_seek_next`` returns silently when the flag is set -- so neither
        ``finished`` nor ``failed`` ever arrived and the dialog hung
        forever.  The PyAV path already does the right thing
        (``request_stop()`` plus ``done.connect(self.finished)``); this
        makes the QtMultimedia path behave the same way.

        Frames already handed over are kept, which is what the task book
        asks for: "支持取消，取消后保留已经明确保存的文件".
        """
        if self._settled:
            return
        self._settled = True
        self._cancelled = True
        kept = self._kept
        total = len(self._timestamps) if self._timestamps else 0
        self._teardown()
        self.finished.emit(kept, total)

    # ── Player callbacks ──────────────────────────────────────

    def _on_error(self, error, message=''):
        if self._settled:
            return
        self._settled = True
        self._teardown()
        self.failed.emit(message or str(error))

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            # Loading can happen more than once.  A walk that seeks near
            # the end lets playback run off the end of the file, and Qt
            # then reloads it and reports LoadedMedia again.  Re-planning
            # would put the walk back at the first timestamp, so the
            # extraction would restart forever and never finish.
            if not self._planned:
                self._planned = True
                self._plan_timestamps()
        elif status in (QMediaPlayer.MediaStatus.InvalidMedia,
                        QMediaPlayer.MediaStatus.NoMedia):
            if not self._planned and not self._settled:
                self._settled = True
                self._teardown()
                self.failed.emit(_('This video could not be opened.'))
        elif status == QMediaPlayer.MediaStatus.EndOfMedia and self._waiting:
            # Playback ran off the end while a seek was still in flight.
            # The pending timestamp cannot be delivered, so log it and let
            # the seek timeout carry the walk on; some files only index
            # the first part of their timeline and behave this way for
            # every position past that point.
            logger.debug('Playback reached the end while seeking to %s',
                         self._timestamps[self._index])

    def _plan_timestamps(self):
        duration = self._player.duration()
        if duration <= 0:
            self._settled = True
            self._teardown()
            self.failed.emit(_('This video reports no duration.'))
            return
        # The media is loaded, so length/size/frame rate are known here
        # without an extra round trip.
        self.video_info.emit(_info_from_player(self._player))
        self._timestamps = list(range(0, duration, self._interval_ms))
        if not self._timestamps:
            self._timestamps = [0]
        self._index = 0
        QtCore.QTimer.singleShot(0, self._seek_next)

    # ── Frame walk ────────────────────────────────────────────

    def _seek_next(self):
        if self._cancelled:
            return
        if self._index >= len(self._timestamps):
            self._settled = True
            kept = self._kept
            total = len(self._timestamps)
            self._teardown()
            if kept:
                self.finished.emit(kept, total)
            else:
                self.failed.emit(_(
                    'No usable frames were decoded. Check that the video '
                    'plays, then try again; nothing was created.'))
            return

        self.progress.emit(self._index, len(self._timestamps))
        self.position_changed.emit(self._timestamps[self._index])
        self._waiting = True
        self._current_image = None
        self._player.setPosition(self._timestamps[self._index])
        self._player.play()

        if self._timeout is not None:
            self._timeout.stop()
        self._timeout = QtCore.QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_seek_timeout)
        self._timeout.start(SEEK_TIMEOUT_MS)

    def _on_seek_timeout(self):
        # A seek that never produces a frame must not stall the walk.
        logger.debug('Seek %s timed out', self._timestamps[self._index])
        self._advance()

    def _on_frame(self, frame):
        if not self._waiting or self._cancelled or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._current_image = image
        if self._timeout is not None:
            self._timeout.stop()
        self._advance()

    def _advance(self):
        self._waiting = False
        image = self._current_image
        position = self._timestamps[self._index]
        self._current_image = None
        if image is not None and not self._cancelled:
            self._offer(image, position)
        self._index += 1
        QtCore.QTimer.singleShot(0, self._seek_next)

    def _offer(self, image, position_ms):
        """Keep the frame unless it looks like one we already kept.

        The structure hash alone is not enough: two frames can share a
        layout while showing completely different colours, and a difference
        hash only looks at the brightness pattern.  The colour signature is
        what tells those apart, exactly as duplicate_groups() does for
        stills.
        """
        if self._dedup:
            value = difference_hash(image)
            colour = average_color(image)
            if value is not None:
                for previous_hash, previous_colour in self._hashes[-COMPARE_WINDOW:]:
                    if (hamming_distance(value, previous_hash)
                            <= DUPLICATE_DISTANCE
                            and color_distance(colour, previous_colour)
                            <= COLOR_DISTANCE):
                        logger.trace('Dropped a near-duplicate frame')
                        return
                self._hashes.append((value, colour))
        self._kept += 1
        # The timestamp travels with the frame so the caller can record
        # where in the video it came from.
        self.kept_frame.emit((image, position_ms))

    # ── Lifecycle ─────────────────────────────────────────────

    def _teardown(self):
        if self._timeout is not None:
            self._timeout.stop()
            self._timeout = None
        if self._player is not None:
            try:
                self._player.setSource(QtCore.QUrl())
            except RuntimeError:
                pass
        for signal, slot in (
                (getattr(self._sink, 'videoFrameChanged', None),
                 self._on_frame),):
            if signal is None:
                continue
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for obj in (self._player, self._sink, self._audio):
            if obj is not None:
                obj.deleteLater()
        self._player = None
        self._sink = None
        self._audio = None
        self._waiting = False


def frame_to_pixmap(image):
    """Convert a decoded frame into a pixmap ready for a canvas item."""
    return QtGui.QPixmap.fromImage(image)
